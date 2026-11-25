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
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao import shared_brain as sb
from ciao.config import SharedBrainLink
from ciao.git_proc import GIT_TIMEOUT_DETAIL, run_git as real_run_git

IDENT = ("-c", "user.name=Test", "-c", "user.email=test@example.com")
GITHUB_URL = "https://github.com/x/notes"


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


def _install_registry(config: "FakeConfig", *workspaces: object) -> bytes:
    """Write authoritative registry bytes containing these workspace links."""
    rows = []
    for workspace in workspaces:
        rows.append(
            {
                "name": workspace.name,
                "vault_root": workspace.vault_root,
                "shared_brains": [
                    {
                        "name": link.name,
                        "path": link.path,
                        "identity": link.identity,
                        "lifetime_id": link.lifetime_id,
                        "kind": link.kind,
                        "sharing_policy": link.sharing_policy,
                        "sharing_generation": link.sharing_generation,
                        "sharing_consent": (
                            {
                                "generation": link.sharing_consent.generation,
                                "identity": link.sharing_consent.identity,
                                "lifetime_id": link.sharing_consent.lifetime_id,
                                "granted_at": link.sharing_consent.granted_at,
                            }
                            if link.sharing_consent is not None
                            else None
                        ),
                    }
                    for link in workspace.shared_brains
                ],
            }
        )
    payload = json.dumps(rows).encode()
    path = config.state_path.parent / "workspaces.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    config._workspace_registry_bytes = payload
    return payload


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
def pair(root: Path, monkeypatch) -> SimpleNamespace:
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
    # The clone's origin reads as a hosted address (identity needs one). Fetch and
    # merge are steered back to the local bare repository on the command line, which
    # the config vetting does not read, so the tests run without a network.
    git(clone, "remote", "set-url", "origin", GITHUB_URL)
    monkeypatch.setattr(
        sb,
        "_HARDEN",
        sb._HARDEN
        + ("-c", f"url.{origin}.insteadOf={GITHUB_URL}", "-c", "protocol.file.allow=always"),
    )
    return SimpleNamespace(origin=origin, seed=seed, clone=clone, root=root)


def push_from_seed(pair: SimpleNamespace, filename: str) -> str:
    (pair.seed / filename).write_text(filename + "\n", encoding="utf-8")
    git(pair.seed, "add", ".")
    git(pair.seed, "commit", "-q", "-m", filename)
    git(pair.seed, "push", "-q", "origin", "main")
    return git(pair.seed, "rev-parse", "HEAD")


ENVS: list = []


def link_of(path: Path | str) -> SharedBrainLink:
    """A link to a pair fixture's clone; its hosted origin reads as github.com/x/notes."""
    return SharedBrainLink(name="notes", path=str(Path(path)), identity="github.com/x/notes")


@pytest.fixture
def isolated_update_lifecycle(monkeypatch):
    """Give lifecycle tests fresh process-owned task registries."""
    monkeypatch.setattr(sb, "_pending", {})
    monkeypatch.setattr(sb, "_updates_closed", False)
    monkeypatch.setattr(sb, "_update_loop_task", None)
    monkeypatch.setattr(sb, "_shutdown_task", None)


def record_git(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []
    real = sb.run_git

    async def recording(workspace, *args, timeout=None, env=None):
        calls.append(list(args))
        ENVS.append(env)
        return await real(workspace, *args, timeout=timeout, env=env)

    monkeypatch.setattr(sb, "run_git", recording)
    return calls


@pytest.mark.asyncio
async def test_shutdown_owns_periodic_and_deduplicated_scheduled_updates(
    tmp_path: Path, monkeypatch, isolated_update_lifecycle
) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()
    calls = 0

    async def slow_update(link, config):
        nonlocal calls
        calls += 1
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(sb, "update_brain", slow_update)
    monkeypatch.setattr(sb, "FIRST_UPDATE_DELAY_S", 3600)
    link = link_of(tmp_path / "brain")
    config = SimpleNamespace(workspaces={})

    periodic = sb.start_update_loop(config)
    assert sb.start_update_loop(config) is periodic
    sb.schedule_update(link, config)
    sb.schedule_update(link, config)
    assert len(sb._pending) == 1
    await started.wait()

    shutdown = asyncio.create_task(sb.shutdown_updates())
    await asyncio.sleep(0)
    sb.schedule_update(link_of(tmp_path / "late"), config)
    await shutdown

    assert calls == 1
    assert cancelled.is_set()
    assert periodic.done()
    assert not sb._pending


@pytest.mark.asyncio
async def test_shutdown_awaits_a_scheduled_job_that_fails_during_cancellation(
    tmp_path: Path, monkeypatch, isolated_update_lifecycle
) -> None:
    started = asyncio.Event()

    async def broken_update(link, config):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            raise RuntimeError("update teardown failed") from exc

    monkeypatch.setattr(sb, "update_brain", broken_update)
    config = SimpleNamespace(workspaces={})
    sb.schedule_update(link_of(tmp_path / "brain"), config)
    await started.wait()

    await sb.shutdown_updates()

    assert not sb._pending


@pytest.mark.asyncio
async def test_cancellation_of_shutdown_waits_for_owned_task_cleanup(
    tmp_path: Path, monkeypatch, isolated_update_lifecycle
) -> None:
    started = asyncio.Event()
    cancelling = asyncio.Event()
    finish_cleanup = asyncio.Event()

    async def slow_cleanup(link, config):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelling.set()
            await finish_cleanup.wait()
            raise

    monkeypatch.setattr(sb, "update_brain", slow_cleanup)
    sb.schedule_update(link_of(tmp_path / "brain"), SimpleNamespace(workspaces={}))
    await started.wait()
    shutdown = asyncio.create_task(sb.shutdown_updates())
    await cancelling.wait()

    shutdown.cancel()
    await asyncio.sleep(0)
    assert not shutdown.done()

    finish_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await shutdown
    assert not sb._pending


@pytest.mark.asyncio
async def test_shutdown_cancels_periodic_and_scheduled_jobs_contending_on_checkout(
    tmp_path: Path, monkeypatch, isolated_update_lifecycle
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def blocked_update(config, folder, identity):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return sb._result("up_to_date")

    monkeypatch.setattr(sb, "_update_unlocked", blocked_update)
    monkeypatch.setattr(sb, "FIRST_UPDATE_DELAY_S", 0)
    monkeypatch.setattr(sb, "UPDATE_INTERVAL_S", 3600)
    path = tmp_path / "brain"
    path.mkdir()
    link = link_of(path)
    config = FakeConfig(tmp_path)
    config.workspaces = {"personal": SimpleNamespace(shared_brains=[link])}

    sb.schedule_update(link, config)
    await entered.wait()
    periodic = sb.start_update_loop(config)
    await asyncio.sleep(0)
    assert calls == 1  # the periodic update is waiting on this checkout's lock

    await sb.shutdown_updates()

    assert periodic.done()
    assert not sb._pending
    assert calls == 1


@pytest.mark.skipif(sys.version_info[:2] != (3, 12), reason="Python 3.12 subprocess teardown regression")
def test_shutdown_reaps_subprocess_pipes_before_asyncio_run_teardown() -> None:
    """Cancellation while Python 3.12 connects subprocess pipes is joined."""
    script = r'''
import asyncio
import sys
from pathlib import Path
from ciao import shared_brain as sb

pipe_setup = asyncio.Event()
finish_pipe_setup = asyncio.Event()
loop = None
connect_read_pipe = None

async def pause_during_pipe_setup(protocol_factory, pipe):
    pipe_setup.set()
    await finish_pipe_setup.wait()
    return await connect_read_pipe(protocol_factory, pipe)

async def update(_link, _config):
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(30)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
    finally:
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.communicate()

async def main():
    global loop, connect_read_pipe
    loop = asyncio.get_running_loop()
    connect_read_pipe = loop.connect_read_pipe
    loop.connect_read_pipe = pause_during_pipe_setup
    sb.update_brain = update
    sb.schedule_update(type("Link", (), {"path": str(Path("/tmp/ciao-shutdown-pipe")), "kind": "git"})(), None)
    await pipe_setup.wait()
    shutdown = asyncio.create_task(sb.shutdown_updates())
    await asyncio.sleep(0)
    if shutdown.done():
        raise AssertionError("shutdown returned before pipe setup was released")
    finish_pipe_setup.set()
    await shutdown

asyncio.run(main())
'''
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.asyncio
async def test_a_new_server_lifetime_reopens_admission_after_shutdown(
    tmp_path: Path, monkeypatch, isolated_update_lifecycle
) -> None:
    calls = 0

    async def update(link, config):
        nonlocal calls
        calls += 1
        return sb._result("up_to_date")

    monkeypatch.setattr(sb, "update_brain", update)
    config = SimpleNamespace(workspaces={})
    link = link_of(tmp_path / "brain")

    sb.schedule_update(link, config)
    await sb.shutdown_updates()
    sb.begin_update_lifecycle()
    sb.schedule_update(link, config)
    await asyncio.sleep(0)
    await sb.shutdown_updates()

    assert calls == 2


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

    state = sb._brain_state_owned(link, FakeConfig(root))
    assert state["missing"] is False and state["branch"] == "main"
    assert state["head"] == head[:7] and state["head_date"]
    assert state["upstream"] is True and state["dirty"] is False
    assert state["origin_changed"] is False
    assert state["last_update"] is None

    (checkout / "a.md").write_text("edited\n", encoding="utf-8")
    assert sb._brain_state_owned(link, FakeConfig(root))["dirty"] is True

    git(checkout, "remote", "set-url", "origin", "https://github.com/x/elsewhere")
    assert sb._brain_state_owned(link, FakeConfig(root))["origin_changed"] is True

    gone = SharedBrainLink(name="gone", path=str(root / "gone"), identity=link.identity)
    assert sb._brain_state_owned(gone, FakeConfig(root))["missing"] is True


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
    result = await sb.update_brain(link_of(root / "nowhere"), FakeConfig(root))
    _assert_result(result, "failed", "missing")
    assert calls == []


@pytest.mark.asyncio
async def test_update_row2_detached(pair: SimpleNamespace) -> None:
    git(pair.clone, "checkout", "-q", "--detach")
    head = git(pair.clone, "rev-parse", "HEAD")
    _assert_result(await sb.update_brain(link_of(pair.clone), FakeConfig(pair.root)), "skipped", "detached")
    assert git(pair.clone, "rev-parse", "HEAD") == head


@pytest.mark.asyncio
async def test_update_row3_no_upstream(pair: SimpleNamespace) -> None:
    git(pair.clone, "checkout", "-q", "-b", "local-only")
    head = git(pair.clone, "rev-parse", "HEAD")
    _assert_result(await sb.update_brain(link_of(pair.clone), FakeConfig(pair.root)), "skipped", "no_upstream")
    assert git(pair.clone, "rev-parse", "HEAD") == head


@pytest.mark.asyncio
async def test_update_row4_fetch_failed_redacts_and_caps(pair: SimpleNamespace, monkeypatch) -> None:
    async def failing(workspace, *args, timeout=None, env=None):
        if "fetch" in args:
            return (128, "", "fatal: cannot reach https://user:SECRETVALUE123@host/x " + "y" * 900)
        return await real_run_git(workspace, *args, timeout=timeout, env=env)

    monkeypatch.setattr(sb, "run_git", failing)
    result = await sb.update_brain(link_of(pair.clone), FakeConfig(pair.root))
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
    _assert_result(await sb.update_brain(link_of(pair.clone), FakeConfig(pair.root)), "failed", "timeout")


@pytest.mark.asyncio
@pytest.mark.parametrize("dirty", [False, True])
async def test_update_row6_ahead_or_equal_is_up_to_date(pair: SimpleNamespace, dirty: bool) -> None:
    (pair.clone / "local.md").write_text("mine\n", encoding="utf-8")
    git(pair.clone, "add", ".")
    git(pair.clone, "commit", "-q", "-m", "local")
    if dirty:
        (pair.clone / "a.md").write_text("uncommitted\n", encoding="utf-8")
    head = git(pair.clone, "rev-parse", "HEAD")
    _assert_result(await sb.update_brain(link_of(pair.clone), FakeConfig(pair.root)), "up_to_date", "")
    assert git(pair.clone, "rev-parse", "HEAD") == head
    assert (git(pair.clone, "status", "--porcelain") != "") is dirty


@pytest.mark.asyncio
async def test_update_row7_diverged(pair: SimpleNamespace) -> None:
    (pair.clone / "local.md").write_text("mine\n", encoding="utf-8")
    git(pair.clone, "add", ".")
    git(pair.clone, "commit", "-q", "-m", "local")
    push_from_seed(pair, "remote.md")
    head = git(pair.clone, "rev-parse", "HEAD")
    _assert_result(await sb.update_brain(link_of(pair.clone), FakeConfig(pair.root)), "failed", "diverged")
    assert git(pair.clone, "rev-parse", "HEAD") == head


@pytest.mark.asyncio
async def test_update_row8_behind_with_local_changes_is_skipped(pair: SimpleNamespace) -> None:
    remote_head = push_from_seed(pair, "remote.md")
    (pair.clone / "a.md").write_text("edited\n", encoding="utf-8")
    head = git(pair.clone, "rev-parse", "HEAD")
    _assert_result(await sb.update_brain(link_of(pair.clone), FakeConfig(pair.root)), "skipped", "local_changes")
    assert git(pair.clone, "rev-parse", "HEAD") == head
    assert git(pair.clone, "rev-parse", "origin/main") == remote_head
    state = sb._brain_state_owned(link_of(pair.clone), FakeConfig(pair.root))
    assert state["head"] == head[:7]
    assert state["fetched_at"]
    assert state["last_update"]["outcome"] == "skipped"
    assert state["last_update"]["reason"] == "local_changes"
    assert (pair.clone / "a.md").read_text(encoding="utf-8") == "edited\n"


@pytest.mark.asyncio
async def test_update_row9_behind_clean_fast_forwards(pair: SimpleNamespace) -> None:
    new_head = push_from_seed(pair, "remote.md")
    (pair.clone / ".obsidian").mkdir()
    (pair.clone / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
    (pair.clone / ".DS_Store").write_text("x", encoding="utf-8")
    _assert_result(await sb.update_brain(link_of(pair.clone), FakeConfig(pair.root)), "updated", "")
    assert git(pair.clone, "rev-parse", "HEAD") == new_head
    assert git(pair.clone, "status", "--porcelain", "--untracked-files=no") == ""
    assert (pair.clone / ".obsidian" / "app.json").exists()


@pytest.mark.asyncio
async def test_update_row10_merge_failed_leaves_head(pair: SimpleNamespace) -> None:
    push_from_seed(pair, "collide.md")
    (pair.clone / "collide.md").write_text("untracked local\n", encoding="utf-8")
    head = git(pair.clone, "rev-parse", "HEAD")
    result = await sb.update_brain(link_of(pair.clone), FakeConfig(pair.root))
    _assert_result(result, "failed", "merge_failed")
    assert git(pair.clone, "rev-parse", "HEAD") == head
    assert (pair.clone / "collide.md").read_text(encoding="utf-8") == "untracked local\n"


@pytest.mark.asyncio
async def test_update_never_runs_a_rewriting_git_command(pair: SimpleNamespace, monkeypatch) -> None:
    calls = record_git(monkeypatch)
    push_from_seed(pair, "remote.md")
    await sb.update_brain(link_of(pair.clone), FakeConfig(pair.root))
    (pair.clone / "a.md").write_text("edited\n", encoding="utf-8")
    push_from_seed(pair, "more.md")
    await sb.update_brain(link_of(pair.clone), FakeConfig(pair.root))
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
    await asyncio.gather(sb.update_brain(link_of(pair.clone), FakeConfig(pair.root)), sb.update_brain(link_of(pair.clone), FakeConfig(pair.root)))
    assert [e.split()[0] for e in events] == ["start", "end", "start", "end"]


@pytest.mark.asyncio
async def test_update_loop_updates_each_distinct_path_once(root: Path, monkeypatch) -> None:
    monkeypatch.setattr(sb, "FIRST_UPDATE_DELAY_S", 0)
    monkeypatch.setattr(sb, "UPDATE_INTERVAL_S", 3600)
    seen: list[str] = []

    async def record(link, config=None) -> dict:
        seen.append(link.path)
        return {"outcome": "up_to_date", "reason": "", "detail": "", "at": ""}

    monkeypatch.setattr(sb, "update_brain", record)
    config = FakeConfig(Path("/zz-install"))
    config.workspaces = {
        "a": SimpleNamespace(shared_brains=[SharedBrainLink("x", "/p/one", "h/o/x")]),
        "b": SimpleNamespace(
            shared_brains=[
                SharedBrainLink("x", "/p/one", "h/o/x"),
                SharedBrainLink("y", "/p/two", "h/o/y"),
            ]
        ),
    }
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

    async def record(link, config=None) -> dict:
        seen.append(link.path)
        return {"outcome": "up_to_date", "reason": "", "detail": "", "at": ""}

    monkeypatch.setattr(sb, "update_brain", record)
    links = [SharedBrainLink(f"n{i}", f"/p/{i}", f"h/o/{i}") for i in range(3)]
    config = FakeConfig(Path("/zz-install"))
    config.workspaces = {"a": SimpleNamespace(shared_brains=links)}
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
    git(fine, "remote", "set-url", "origin", GITHUB_URL)
    push_from_seed(pair, "remote.md")  # both clones are now behind

    real = sb.run_git

    async def timing_out_first(workspace, *args, timeout=None, env=None):
        if "fetch" in args and str(workspace) == str(pair.clone):
            return (-1, "", GIT_TIMEOUT_DETAIL)
        return await real(workspace, *args, timeout=timeout, env=env)

    monkeypatch.setattr(sb, "run_git", timing_out_first)
    counts = await sb._run_pass(FakeConfig(pair.root), [link_of(pair.clone), link_of(fine)])
    assert counts == {"failed": 1, "updated": 1}
    assert git(fine, "rev-parse", "HEAD") == git(pair.seed, "rev-parse", "HEAD")


def test_schedule_update_keeps_a_reference_until_done(monkeypatch) -> None:
    async def quick(link, config=None) -> dict:
        return {"outcome": "up_to_date", "reason": "", "detail": "", "at": ""}

    monkeypatch.setattr(sb, "update_brain", quick)

    async def run() -> None:
        sb.schedule_update(SharedBrainLink("x", "/p/x", "github.com/x/x"), FakeConfig(Path("/p")))
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
    git(brain, "remote", "set-url", "origin", GITHUB_URL)
    shutil.rmtree(brain / ".git")
    parent_head = git(parent, "rev-parse", "HEAD")
    calls = record_git(monkeypatch)

    result = await sb.update_brain(link_of(brain), FakeConfig(pair.root))

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

    result = await sb.update_brain(SharedBrainLink(name='notes', path=stored, identity='github.com/x/notes'), FakeConfig(pair.root))

    assert result["reason"] == "moved"
    assert calls == []


@pytest.mark.asyncio
async def test_runtime_refuses_a_folder_that_moved_into_a_vault(pair: SimpleNamespace) -> None:
    vault = pair.root / "vaults" / "alice"
    config = FakeConfig(pair.root, {"alice": vault})
    vault.mkdir(parents=True)
    brain = vault / "notes"
    subprocess.run(["git", "clone", "-q", str(pair.origin), str(brain)], check=True)
    result = await sb.update_brain(link_of(brain), config)
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


def test_kind_defaults_to_git_and_invalid_kinds_are_dropped() -> None:
    from ciao.config import shared_brains_from_rows

    rows = [
        {"name": "legacy", "path": "/away/legacy", "identity": "github.com/a/legacy"},
        {"name": "folder", "path": "/away/folder", "identity": "folder:/away/folder", "kind": "folder"},
        {"name": "bad", "path": "/away/bad", "identity": "github.com/a/bad", "kind": "svn"},
    ]
    links = shared_brains_from_rows(rows)
    assert [(link.name, link.kind) for link in links] == [("legacy", "git"), ("folder", "folder")]


def test_explicit_non_string_kind_is_not_treated_as_omitted() -> None:
    from ciao.workspaces import _shared_brain_rows

    with pytest.raises(ValueError, match="shared_brains must be a list"):
        _shared_brain_rows([{"path": "/cloud/team", "kind": None}])


def test_folder_kind_and_lifetime_persist_across_config_reload(tmp_path: Path) -> None:
    from ciao.config import CiaoConfig

    env = {
        "CIAO_WORKSPACE": str(tmp_path),
        "CIAO_RUNTIME_ROOT": str(tmp_path / ".runtime"),
        "PWA_AUTH_TOKEN": "test-token",
    }
    config = CiaoConfig.from_env(env)
    workspace = config.workspace(config.primary_workspace())
    workspace.shared_brains = [SharedBrainLink(
        "team", "/cloud/team", "folder:/cloud/team", lifetime_id="same-lifetime", kind="folder"
    )]
    config.persist_workspace_registry()
    reloaded = CiaoConfig.from_env(env)
    (link,) = reloaded.workspace(workspace.name).shared_brains
    assert link.kind == "folder"
    assert link.lifetime_id == "same-lifetime"


def test_folder_link_validation_retention_and_git_free_state(tmp_path: Path, monkeypatch) -> None:
    folder = tmp_path / "cloud folder"
    folder.mkdir()
    config = FakeConfig(tmp_path)
    monkeypatch.setattr(sb, "_git", lambda *_args: pytest.fail("folder links must not invoke git"))
    (link,) = sb.resolve_links(config, "work", [{"path": str(folder), "kind": "folder"}], [])
    assert link.path == str(folder.resolve())
    assert link.identity == f"folder:{folder.resolve()}"
    assert link.kind == "folder"
    assert sb._brain_state_owned(link, config)["problem"] == ""

    (retained,) = sb.resolve_links(config, "work", [{"path": str(folder)}], [link])
    assert retained == link
    with pytest.raises(ValueError, match="unlink and add it again"):
        sb.resolve_links(config, "work", [{"path": str(folder), "kind": "git"}], [link])
    assert sb.resolve_links(config, "work", [], [link]) == []
    monkeypatch.undo()
    make_checkout(folder, "https://github.com/acme/relinked")
    (relinked,) = sb.resolve_links(config, "work", [{"path": str(folder), "kind": "git"}], [])
    assert relinked.kind == "git"
    assert relinked.lifetime_id != link.lifetime_id


@pytest.mark.parametrize("order", [("folder", "git"), ("git", "folder")])
def test_retained_links_recheck_accumulated_names_and_identities(tmp_path: Path, order: tuple[str, str]) -> None:
    folder = tmp_path / "shared"
    folder.mkdir()
    checkout = make_checkout(tmp_path / "checkout", "https://github.com/acme/notes")
    folder_link = SharedBrainLink("notes", str(folder), f"folder:{folder}", kind="folder")
    git_link = SharedBrainLink("notes", str(checkout), "github.com/acme/notes")
    by_kind = {"folder": (folder_link, {"path": str(folder), "kind": "folder"}),
               "git": (git_link, {"path": str(checkout), "kind": "git"})}
    existing = [by_kind[key][0] for key in order]
    rows = [by_kind[key][1] for key in order]
    config = FakeConfig(tmp_path)
    with pytest.raises(ValueError, match="duplicated"):
        sb.resolve_links(config, "work", rows, existing)


@pytest.mark.parametrize("new_first", [False, True])
def test_new_and_retained_links_recheck_duplicate_name_and_identity(tmp_path: Path, new_first: bool) -> None:
    folder = tmp_path / "retained"
    folder.mkdir()
    retained = SharedBrainLink("notes", str(folder), f"folder:{folder}", kind="folder")
    new_path = tmp_path / "other"
    new_path.mkdir()
    new = {"path": str(new_path), "kind": "folder", "name": "notes"}
    entries = [(retained, {"path": str(folder), "kind": "folder"}), (None, new)]
    if new_first:
        entries.reverse()
    config = FakeConfig(tmp_path)
    with pytest.raises(ValueError, match="already used|duplicated"):
        sb.resolve_links(config, "work", [row for _link, row in entries], [retained])


def test_folder_and_git_links_can_coexist(tmp_path: Path, monkeypatch) -> None:
    folder = tmp_path.parent / f"{tmp_path.name}-team-folder"
    folder.mkdir()
    checkout = make_checkout(tmp_path / "git-brain", "https://github.com/acme/git-brain")
    config = FakeConfig(tmp_path)
    links = sb.resolve_links(config, "work", [
        {"path": str(folder), "kind": "folder"},
        {"path": str(checkout), "kind": "git"},
    ], [])
    assert [link.kind for link in links] == ["folder", "git"]


def test_folder_links_reject_missing_protected_and_duplicate_paths(tmp_path: Path) -> None:
    folder = tmp_path.parent / f"{tmp_path.name}-shared"
    folder.mkdir()
    config = FakeConfig(tmp_path)
    config.workspace_root.mkdir(parents=True)
    with pytest.raises(ValueError, match="does not exist"):
        sb.resolve_links(config, "work", [{"path": str(tmp_path / "missing"), "kind": "folder"}], [])
    with pytest.raises(ValueError, match="outside the Ciaobot install"):
        sb.resolve_links(config, "work", [{"path": str(config.workspace_root), "kind": "folder"}], [])
    with pytest.raises(ValueError, match="listed twice"):
        sb.resolve_links(config, "work", [
            {"path": str(folder), "kind": "folder"},
            {"path": str(folder), "kind": "folder"},
        ], [])


def test_folder_link_rejects_a_symlink_into_a_protected_root(tmp_path: Path) -> None:
    config = FakeConfig(tmp_path)
    config.workspace_root.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(config.workspace_root, target_is_directory=True)
    with pytest.raises(ValueError, match="outside the Ciaobot install"):
        sb.resolve_links(config, "work", [{"path": str(alias), "kind": "folder"}], [])


def test_folder_link_rejects_inaccessible_directory(tmp_path: Path, monkeypatch) -> None:
    folder = tmp_path / "cloud"
    folder.mkdir()
    monkeypatch.setattr(sb.os, "access", lambda *_args: False)
    with pytest.raises(ValueError, match="not accessible"):
        sb.resolve_links(FakeConfig(tmp_path), "work", [{"path": str(folder), "kind": "folder"}], [])


@pytest.mark.asyncio
async def test_folder_link_never_updates_or_enters_git_update_batch(tmp_path: Path, monkeypatch) -> None:
    folder = tmp_path / "folder"
    folder.mkdir()
    link = SharedBrainLink("brain", str(folder), f"folder:{folder}", kind="folder")
    config = FakeConfig(tmp_path)
    config.workspaces = {"work": SimpleNamespace(shared_brains=[link])}
    monkeypatch.setattr(sb, "_git", lambda *_args: pytest.fail("folder status must not invoke git"))
    result = await sb.update_brain(link, config)
    assert result["outcome"] == "skipped" and result["reason"] == "folder"
    assert sb._linked_links(config) == []
    sb.schedule_update(link, config)
    assert sb._lock_key(link.path) not in sb._pending


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
    assert (await sb.update_brain(link_of(pair.clone), FakeConfig(pair.root)))["outcome"] == "updated"
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

    async def slow(link, config=None):
        started.append(link.path)
        await asyncio.sleep(0.05)
        return {"outcome": "up_to_date", "reason": "", "detail": "", "at": ""}

    monkeypatch.setattr(sb, "update_brain", slow)

    async def run() -> None:
        sb.schedule_update(SharedBrainLink("x", str(tmp_path / "x"), "github.com/x/x"), FakeConfig(tmp_path))
        sb.schedule_update(SharedBrainLink("x", str(tmp_path / "x"), "github.com/x/x"), FakeConfig(tmp_path))
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


# --- second review: worktree scope, tampering after link, GIT_* env, pointers ---


@pytest.mark.parametrize("key,value", [("filter.x.smudge", "cat"), ("core.sshCommand", "ssh")])
def test_link_refuses_worktree_scoped_program_config(root: Path, key: str, value: str) -> None:
    checkout = make_checkout(root / "wt-hostile", "https://github.com/x/wt")
    git(checkout, "config", "extensions.worktreeConfig", "true")
    git(checkout, "config", "--worktree", key, value)
    with pytest.raises(ValueError, match="can run programs"):
        sb.resolve_links(FakeConfig(root), "work", [{"path": str(checkout)}], [])


@pytest.mark.parametrize(
    "key,value",
    [
        ("core.alternateRefsCommand", "cat"),
        ("gpg.program", "/bin/evil"),
        ("merge.verifySignatures", "true"),
        ("core.pager", "/bin/evil"),
        ("core.editor", "/bin/evil"),
        ("diff.external", "/bin/evil"),
        ("url.file:///tmp/evil.insteadOf", "https://github.com/x/notes"),
        ("url.file:///tmp/evil.pushInsteadOf", "https://github.com/x/notes"),
    ],
)
def test_link_refuses_more_program_and_redirect_keys(root: Path, key: str, value: str) -> None:
    checkout = make_checkout(root / "more-hostile", "https://github.com/x/more")
    git(checkout, "config", key, value)
    with pytest.raises(ValueError, match="can run programs"):
        sb.resolve_links(FakeConfig(root), "work", [{"path": str(checkout)}], [])


@pytest.mark.asyncio
async def test_update_refuses_config_tampered_after_linking(pair: SimpleNamespace, monkeypatch) -> None:
    push_from_seed(pair, "remote.md")
    git(pair.clone, "config", "core.sshCommand", "/bin/evil")
    calls = record_git(monkeypatch)
    result = await sb.update_brain(link_of(pair.clone), FakeConfig(pair.root))
    assert result["outcome"] == "failed" and result["reason"] == "unsafe_config"
    assert not [a for a in calls if "fetch" in a or "merge" in a]


@pytest.mark.asyncio
async def test_restored_row_with_hostile_config_is_refused_at_update(
    pair: SimpleNamespace, monkeypatch
) -> None:
    from ciao.config import shared_brains_from_rows

    push_from_seed(pair, "remote.md")
    git(pair.clone, "config", "filter.x.smudge", "cat")
    row = {"name": "notes", "path": str(pair.clone.resolve()), "identity": "github.com/x/notes"}
    (link,) = shared_brains_from_rows([row])
    calls = record_git(monkeypatch)
    result = await sb.update_brain(link, FakeConfig(pair.root))
    assert result["reason"] == "unsafe_config"
    assert not [a for a in calls if "fetch" in a or "merge" in a]


def test_brain_state_reports_unsafe_config(pair: SimpleNamespace) -> None:
    git(pair.clone, "config", "core.pager", "/bin/evil")
    link = sb.SharedBrainLink(name="notes", path=str(pair.clone.resolve()), identity="github.com/x/notes")
    assert sb._brain_state_owned(link, FakeConfig(pair.root))["problem"] == "unsafe_config"


@pytest.mark.asyncio
async def test_git_dir_pointer_is_refused_at_update_and_link(pair: SimpleNamespace, monkeypatch) -> None:
    worktree = pair.root / "linked-wt"
    git(pair.clone, "worktree", "add", "-q", str(worktree), "-b", "wt-branch")
    assert (worktree / ".git").is_file()
    with pytest.raises(ValueError, match="fresh clone"):
        sb.resolve_links(FakeConfig(pair.root), "work", [{"path": str(worktree)}], [])
    calls = record_git(monkeypatch)
    result = await sb.update_brain(link_of(worktree), FakeConfig(pair.root))
    assert result["reason"] == "not_a_checkout"
    assert not [a for a in calls if "fetch" in a or "merge" in a]


@pytest.mark.asyncio
async def test_engine_git_dir_does_not_redirect_the_update(pair: SimpleNamespace, monkeypatch) -> None:
    other = make_checkout(pair.root / "other-repo")
    (other / "o.md").write_text("o\n", encoding="utf-8")
    git(other, "add", ".")
    git(other, "commit", "-q", "-m", "other")
    other_head = git(other, "rev-parse", "HEAD")
    new_head = push_from_seed(pair, "remote.md")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    result = await sb.update_brain(link_of(pair.clone), FakeConfig(pair.root))
    assert result["outcome"] == "updated", result
    clean = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

    def head(path: Path) -> str:
        return subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True, text=True, env=clean, check=True,
        ).stdout.strip()

    assert head(pair.clone) == new_head
    assert head(other) == other_head


@pytest.mark.asyncio
async def test_forget_unlinked_keeps_a_locked_entry() -> None:
    lock = asyncio.Lock()
    await lock.acquire()
    sb._locks["/running"] = lock
    try:
        sb._forget_unlinked(set())
        assert "/running" in sb._locks
    finally:
        lock.release()
        sb._locks.pop("/running", None)


def test_load_keeps_a_brain_pointing_into_the_install_and_reports_it(tmp_path: Path) -> None:
    from ciao.config import CiaoConfig

    install = tmp_path / "install"
    registry = install / ".runtime"
    registry.mkdir(parents=True)
    inside = {"name": "inside", "path": str(install / "vaultish"), "identity": "github.com/a/i"}
    (registry / "workspaces.json").write_text(
        json.dumps([{"name": "personal", "vault_root": "personal", "shared_brains": [inside]}]),
        encoding="utf-8",
    )
    config = CiaoConfig.from_env(
        {"CIAO_WORKSPACE": str(install), "CIAO_RUNTIME_ROOT": str(registry), "PWA_AUTH_TOKEN": "t"}
    )
    (link,) = config.workspace("personal").shared_brains
    assert link.name == "inside"
    assert sb._brain_state_owned(link, config)["problem"] == "outside_install"
    assert sb._linked_links(config) == []


# --- third review: origin identity at update, scheme allowlist, order, narrow allowlist ---

SCHEME_REFUSED = [
    "ext::sh -c touch% /tmp/pwned",
    "fd::17",
    "file:///tmp/evil.git",
    "foo::bar",
    "http://github.com/x/notes",
    "git://github.com/x/notes",
    "/abs/local/bare.git",
    "https://github.com/x::y",
]


@pytest.mark.parametrize("url", SCHEME_REFUSED)
def test_canonical_identity_refuses_unlisted_schemes(url: str) -> None:
    with pytest.raises(ValueError, match="unsupported remote address|unrecognised remote address"):
        sb.canonical_identity(url)


@pytest.mark.parametrize(
    "url,identity",
    [
        ("https://github.com/acme/notes.git", "github.com/acme/notes"),
        ("ssh://git@github.com/acme/notes", "github.com/acme/notes"),
        ("git@github.com:acme/notes.git", "github.com/acme/notes"),
    ],
)
def test_canonical_identity_accepts_the_three_shapes(url: str, identity: str) -> None:
    assert sb.canonical_identity(url) == identity


@pytest.mark.parametrize("url", SCHEME_REFUSED)
def test_link_refuses_an_unlisted_scheme_origin(root: Path, url: str) -> None:
    checkout = make_checkout(root / "scheme-origin")
    git(checkout, "remote", "add", "origin", url)
    with pytest.raises(ValueError, match="unsupported remote address|unrecognised remote address"):
        sb.resolve_links(FakeConfig(root), "work", [{"path": str(checkout)}], [])


@pytest.mark.asyncio
@pytest.mark.parametrize("url", SCHEME_REFUSED)
async def test_update_refuses_origin_moved_to_an_unlisted_scheme(
    pair: SimpleNamespace, monkeypatch, url: str
) -> None:
    git(pair.clone, "remote", "set-url", "origin", url)
    calls = record_git(monkeypatch)
    result = await sb.update_brain(link_of(pair.clone), FakeConfig(pair.root))
    assert result["reason"] == "origin_changed"
    assert not [a for a in calls if "fetch" in a or "merge" in a]


@pytest.mark.asyncio
async def test_update_refuses_origin_pointing_at_another_repository(
    pair: SimpleNamespace, monkeypatch
) -> None:
    git(pair.clone, "remote", "set-url", "origin", "https://github.com/x/elsewhere")
    calls = record_git(monkeypatch)
    result = await sb.update_brain(link_of(pair.clone), FakeConfig(pair.root))
    assert result["reason"] == "origin_changed"
    assert not [a for a in calls if "fetch" in a or "merge" in a]


@pytest.mark.asyncio
async def test_case_variant_remote_subsection_is_refused_with_no_fetch(
    pair: SimpleNamespace, monkeypatch
) -> None:
    git(pair.clone, "config", "remote.ORIGIN.url", "https://evil/x.git")
    git(pair.clone, "config", "branch.main.remote", "ORIGIN")
    git(pair.clone, "config", "branch.main.merge", "refs/heads/main")
    calls = record_git(monkeypatch)
    result = await sb.update_brain(link_of(pair.clone), FakeConfig(pair.root))
    assert result["reason"] == "unsafe_config"
    assert not [a for a in calls if "fetch" in a or "merge" in a]
    with pytest.raises(ValueError, match="can run programs"):
        sb.resolve_links(FakeConfig(pair.root), "work", [{"path": str(pair.clone)}], [])


@pytest.mark.asyncio
async def test_branch_remote_other_than_origin_is_refused(pair: SimpleNamespace, monkeypatch) -> None:
    git(pair.clone, "config", "branch.main.remote", "elsewhere")
    calls = record_git(monkeypatch)
    result = await sb.update_brain(link_of(pair.clone), FakeConfig(pair.root))
    assert result["reason"] == "unsafe_config"
    assert not [a for a in calls if "fetch" in a or "merge" in a]


@pytest.mark.asyncio
async def test_config_is_vetted_before_any_other_git_command(pair: SimpleNamespace, monkeypatch) -> None:
    calls = record_git(monkeypatch)
    await sb.update_brain(link_of(pair.clone), FakeConfig(pair.root))
    assert calls and "--show-scope" in calls[0]


def test_brain_state_vets_config_before_other_git(pair: SimpleNamespace, monkeypatch) -> None:
    seen: list[list[str]] = []
    real = sb._git

    def spy(path, *args):
        seen.append(list(args))
        return real(path, *args)

    monkeypatch.setattr(sb, "_git", spy)
    sb._brain_state_owned(link_of(pair.clone), FakeConfig(pair.root))
    assert seen and "--show-scope" in seen[0]


def test_hardening_pins_the_protocol_allowlist() -> None:
    hardened = " ".join(sb._HARDEN)
    for flag in ("protocol.allow=never", "protocol.https.allow=always", "protocol.ssh.allow=always"):
        assert flag in hardened


@pytest.mark.parametrize(
    "key,value",
    [
        ("user.name", "Mallory"),
        ("init.defaultBranch", "main"),
        ("pull.rebase", "true"),
        ("branch.main.mergeOptions", "--no-ff"),
        ("remote.origin.fetch", "+refs/*:refs/*"),
        ("extensions.partialClone", "origin"),
        ("core.autocrlf", "true"),
    ],
)
def test_narrowed_allowlist_refuses_newly_dropped_keys(root: Path, key: str, value: str) -> None:
    checkout = make_checkout(root / "narrow", "https://github.com/x/narrow")
    git(checkout, "config", key, value)
    with pytest.raises(ValueError, match="can run programs"):
        sb.resolve_links(FakeConfig(root), "work", [{"path": str(checkout)}], [])


def test_clone_of_a_local_bare_repo_with_https_origin_still_links(root: Path) -> None:
    origin = root / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = make_checkout(root / "seed2")
    (seed / "a.md").write_text("a\n", encoding="utf-8")
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "a")
    git(seed, "push", "-q", str(origin), "main")
    clone = root / "clone2"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    git(clone, "remote", "set-url", "origin", "https://github.com/acme/notes.git")
    (link,) = sb.resolve_links(FakeConfig(root), "work", [{"path": str(clone)}], [])
    assert link.identity == "github.com/acme/notes"


def test_sharing_policy_defaults_to_review_for_legacy_and_new_links() -> None:
    """Every existing registry row and every new link starts at review."""
    from ciao.config import SharedBrainLink, shared_brain_row, shared_brains_from_rows

    legacy = {
        "name": "notes", "path": "/away/notes",
        "identity": "github.com/a/notes", "lifetime_id": "life-legacy",
    }
    [link] = shared_brains_from_rows([legacy])

    assert link.sharing_policy == "review"
    assert link.sharing_generation == 0
    assert link.sharing_consent is None
    # A default link writes the same row it always did: no invented consent keys.
    assert shared_brain_row(link) == {**legacy, "kind": "git"}
    assert SharedBrainLink(
        name="new", path="/away/new", identity="github.com/a/new",
    ).sharing_policy == "review"


def test_sharing_consent_is_durable_generation_stamped_and_revocable() -> None:
    """A grant survives a restart; revocation and re-grant never reuse a generation."""
    from ciao.config import SharedBrainLink, shared_brain_row, shared_brains_from_rows

    link = SharedBrainLink(
        name="notes", path="/away/notes", identity="github.com/a/notes",
        lifetime_id="life-1",
    )
    assert not sb.sharing_consent_active(link)

    granted = sb.set_sharing_policy(link, "automatic")
    assert sb.sharing_consent_active(granted)
    assert granted.sharing_generation == 1
    row = shared_brain_row(granted)
    assert row["sharing_policy"] == "automatic"
    assert row["sharing_generation"] == 1
    assert row["sharing_consent"] == {
        "generation": 1,
        "identity": "github.com/a/notes",
        "lifetime_id": "life-1",
        "granted_at": granted.sharing_consent.granted_at,  # type: ignore[union-attr]
    }

    [loaded] = shared_brains_from_rows([row])
    assert sb.sharing_consent_active(loaded)
    assert loaded.sharing_generation == 1
    # Repeating the current choice is idempotent: no new generation.
    assert sb.set_sharing_policy(loaded, "automatic") == loaded

    revoked = sb.set_sharing_policy(loaded, "review")
    assert not sb.sharing_consent_active(revoked)
    assert revoked.sharing_generation == 2
    assert revoked.sharing_consent is None
    revoked_row = shared_brain_row(revoked)
    assert revoked_row["sharing_policy"] == "review"
    assert revoked_row["sharing_generation"] == 2
    assert "sharing_consent" not in revoked_row

    [loaded_revoked] = shared_brains_from_rows([revoked_row])
    assert not sb.sharing_consent_active(loaded_revoked)
    regranted = sb.set_sharing_policy(loaded_revoked, "automatic")
    assert regranted.sharing_generation == 3
    assert sb.sharing_consent_active(regranted)


def test_relink_identity_drift_and_generation_drift_never_inherit_consent() -> None:
    """A copied consent cannot follow a new lifetime, a new target or a stale generation."""
    from ciao.config import SharedBrainLink, shared_brain_row, shared_brains_from_rows

    link = SharedBrainLink(
        name="notes", path="/away/notes", identity="github.com/a/notes",
        lifetime_id="life-1",
    )
    row = shared_brain_row(sb.set_sharing_policy(link, "automatic"))

    cases = {
        "relinked": {**row, "lifetime_id": "life-2"},
        "repointed": {**row, "identity": "github.com/other/notes"},
        "future generation": {**row, "sharing_generation": row["sharing_generation"] + 1},
        "unknown policy": {**row, "sharing_policy": "always"},
        "policy list": {**row, "sharing_policy": ["automatic"]},
        "malformed consent": {**row, "sharing_consent": "yes"},
    }
    for label, candidate in cases.items():
        [loaded] = shared_brains_from_rows([candidate])
        assert loaded.sharing_policy == "review", label
        assert loaded.sharing_consent is None, label
        assert not sb.sharing_consent_active(loaded), label


def test_restore_rotates_lifetime_and_clears_consent() -> None:
    link = sb.set_sharing_policy(SharedBrainLink(
        name="notes", path="/away/notes", identity="github.com/a/notes",
        lifetime_id="life-1",
    ), "automatic")

    restored = sb.reset_link_for_restore(link)

    assert restored.name == link.name and restored.path == link.path
    assert restored.identity == link.identity
    assert restored.lifetime_id != link.lifetime_id
    assert restored.sharing_policy == "review"
    assert restored.sharing_generation == link.sharing_generation + 1
    assert restored.sharing_consent is None
    assert not sb.sharing_consent_active(restored)


def test_origin_drift_durably_invalidates_consent(tmp_path, monkeypatch) -> None:
    from ciao.config import WorkspaceConfig

    link = sb.set_sharing_policy(SharedBrainLink(
        name="notes", path=str(tmp_path / "notes"), identity="github.com/a/notes",
        lifetime_id="life-1",
    ), "automatic")
    config = FakeConfig(tmp_path)
    config.workspaces = {"work": WorkspaceConfig(
        name="work", vault_root="memory-vault/work", shared_brains=[link],
    )}
    _install_registry(config, config.workspaces["work"])
    persisted: dict[str, object] = {}

    def capture(config_arg, candidate=None, **kwargs):
        persisted["candidate"] = candidate

    monkeypatch.setattr("ciao.workspaces.persist_workspaces", capture)

    assert sb.invalidate_sharing_on_drift(config, link) is True
    stored = config.workspaces["work"].shared_brains[0]
    assert stored.sharing_policy == "review"
    assert stored.sharing_generation == link.sharing_generation + 1
    assert stored.sharing_consent is None
    assert stored.lifetime_id == link.lifetime_id
    assert persisted["candidate"] is not None

    # A second observation has nothing to invalidate.
    assert sb.invalidate_sharing_on_drift(config, stored) is False


@pytest.mark.asyncio
async def test_update_brain_invalidates_consent_when_origin_drift_is_seen(
    pair: SimpleNamespace, monkeypatch
) -> None:
    from ciao.config import WorkspaceConfig

    link = sb.set_sharing_policy(link_of(pair.clone), "automatic")
    config = FakeConfig(pair.root)
    config.workspaces = {"work": WorkspaceConfig(
        name="work", vault_root="memory-vault/work", shared_brains=[link],
    )}
    _install_registry(config, config.workspaces["work"])
    persisted: list[object] = []
    monkeypatch.setattr(
        "ciao.workspaces.persist_workspaces",
        lambda config_arg, candidate=None, **kwargs: persisted.append(candidate),
    )
    git(pair.clone, "remote", "set-url", "origin", "https://github.com/x/elsewhere")

    state = sb._brain_state_owned(link, config)
    assert state["problem"] == "origin_changed"
    assert state["sharing_policy"] == "review"

    result = await sb.update_brain(link, config)

    assert result["reason"] == "origin_changed"
    stored = config.workspaces["work"].shared_brains[0]
    assert stored.sharing_policy == "review"
    assert stored.sharing_consent is None
    assert persisted


def test_drift_invalidation_ignores_a_delayed_newer_generation(tmp_path, monkeypatch) -> None:
    """An observation of generation 1 must not revoke a newer grant at generation 3."""
    from ciao.config import WorkspaceConfig

    observed = sb.set_sharing_policy(SharedBrainLink(
        name="notes", path=str(tmp_path / "notes"), identity="github.com/a/notes",
        lifetime_id="delayed-lifetime",
    ), "automatic")
    newer = sb.set_sharing_policy(
        sb.set_sharing_policy(observed, "review"), "automatic"
    )
    assert newer.sharing_generation == 3
    config = FakeConfig(tmp_path)
    config.workspaces = {"work": WorkspaceConfig(
        name="work", vault_root="memory-vault/work", shared_brains=[newer],
    )}
    monkeypatch.setattr("ciao.workspaces.persist_workspaces", lambda *a, **k: None)

    assert sb.invalidate_sharing_on_drift(config, observed) is False
    kept = config.workspaces["work"].shared_brains[0]
    assert kept.sharing_generation == 3
    assert sb.sharing_consent_active(kept)


def test_drift_invalidation_covers_the_same_path_in_independent_workspaces(tmp_path, monkeypatch) -> None:
    """Drift is a property of the path, so both independently linked workspaces are retired."""
    from ciao.config import WorkspaceConfig

    shared_path = str(tmp_path / "shared-notes")
    first = sb.set_sharing_policy(SharedBrainLink(
        name="notes", path=shared_path, identity="github.com/a/notes",
        lifetime_id="first-lifetime",
    ), "automatic")
    second = sb.set_sharing_policy(SharedBrainLink(
        name="notes", path=shared_path, identity="github.com/a/notes",
        lifetime_id="second-lifetime",
    ), "automatic")
    config = FakeConfig(tmp_path)
    config.workspaces = {
        "first": WorkspaceConfig(
            name="first", vault_root="memory-vault/first", shared_brains=[first],
        ),
        "second": WorkspaceConfig(
            name="second", vault_root="memory-vault/second", shared_brains=[second],
        ),
    }
    _install_registry(config, *config.workspaces.values())
    persist_calls: list[object] = []
    monkeypatch.setattr(
        "ciao.workspaces.persist_workspaces",
        lambda config_arg, candidate=None, **kwargs: persist_calls.append(candidate),
    )

    assert sb.invalidate_sharing_on_drift(config, first) is True
    assert not sb.sharing_consent_active(config.workspaces["first"].shared_brains[0])
    assert not sb.sharing_consent_active(config.workspaces["second"].shared_brains[0])
    assert persist_calls
@pytest.mark.asyncio
async def test_queued_git_update_rechecks_registry_after_folder_adoption(tmp_path, monkeypatch):
    config = FakeConfig(tmp_path)
    checkout = tmp_path / "external"
    checkout.mkdir()
    link = link_of(checkout)
    registry = config.state_path.parent / "workspaces.json"
    registry.parent.mkdir(parents=True)
    registry.write_text("[]")
    config._workspace_registry_bytes = registry.read_bytes()
    lock = sb._checkout_lock(str(checkout))
    monkeypatch.setattr(sb, "_update_unlocked", lambda *args: pytest.fail("Git ran on a folder"))
    async with lock:
        waiting = asyncio.create_task(sb.update_brain(link, config))
        await asyncio.sleep(0)
        registry.write_text(json.dumps([{"name": "work", "shared_brains": [
            {"path": str(checkout), "kind": "folder"}
        ]}]))
    assert (await waiting)["reason"] == "registry_changed"


def test_revocation_journal_is_compacted_and_bounded(tmp_path, monkeypatch) -> None:
    from ciao.config import WorkspaceConfig, SharedBrainLink

    monkeypatch.setattr("ciao.workspaces.persist_workspaces", lambda *a, **k: None)

    link = sb.set_sharing_policy(SharedBrainLink(
        name="notes", path=str(tmp_path / "notes"), identity="github.com/a/notes",
        lifetime_id="live-lifetime",
    ), "automatic")
    config = FakeConfig(tmp_path)
    config.workspaces = {"work": WorkspaceConfig(
        name="work", vault_root="memory-vault/work", shared_brains=[link],
    )}
    registry_bytes = json.dumps([
        {
            "name": "work",
            "shared_brains": [
                {
                    "name": "notes",
                    "path": str(tmp_path / "notes"),
                    "identity": "github.com/a/notes",
                    "lifetime_id": "live-lifetime",
                    "kind": "git",
                    "sharing_policy": "automatic",
                    "sharing_generation": 1,
                    "sharing_consent": {
                        "generation": 1,
                        "identity": "github.com/a/notes",
                        "lifetime_id": "live-lifetime",
                        "granted_at": "2026-11-25T09:00:00Z",
                    },
                }
            ],
        }
    ]).encode()

    sb.save_sharing_revocations(
        config,
        {("live-lifetime", 1), ("repaired-earlier", 0)},
        registry_bytes=registry_bytes,
    )
    state = sb.load_sharing_revocations(config)
    assert state.healthy and not state.fail_closed
    assert state.keys == frozenset({("live-lifetime", 1)})

    # Over the bound without registry compaction: write a bounded fail-closed
    # journal rather than silently dropping entries that might still matter.
    sb.save_sharing_revocations(
        config,
        {(f"lifetime-{i}", 1) for i in range(sb.MAX_SHARING_REVOCATIONS + 1)},
        registry_bytes=None,
    )
    state = sb.load_sharing_revocations(config)
    assert state.healthy and state.fail_closed
    assert state.keys == frozenset()
    sb.apply_persisted_sharing_revocations(config)
    assert config._sharing_recovery_pending is True
    assert sb.recover_sharing_state(config) is True
    assert config.workspaces["work"].shared_brains[0].sharing_policy == "review"


@pytest.mark.asyncio
async def test_cancel_during_update_invalidation_keeps_the_checkout_claim(
    pair: SimpleNamespace, monkeypatch
) -> None:
    import threading
    from ciao.config import WorkspaceConfig

    link = sb.set_sharing_policy(link_of(pair.clone), "automatic")
    config = FakeConfig(pair.root)
    config.workspaces = {"work": WorkspaceConfig(
        name="work", vault_root="memory-vault/work", shared_brains=[link],
    )}
    git(pair.clone, "remote", "set-url", "origin", "https://github.com/x/elsewhere")
    entered, finish = threading.Event(), threading.Event()

    def slow_invalidate(_config, _link, _owner=None):
        entered.set()
        assert finish.wait(5)
        return True

    monkeypatch.setattr(sb, "invalidate_sharing_on_drift", slow_invalidate)
    task = asyncio.create_task(sb.update_brain(link, config))
    assert await asyncio.to_thread(entered.wait, 3)
    for _ in range(2):
        task.cancel()
        await asyncio.sleep(0)
        assert sb._checkout_lock(link.path).locked()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not sb._checkout_lock(link.path).locked()


def test_unreadable_revocation_journal_fails_closed(tmp_path) -> None:
    from ciao.config import SharedBrainLink, WorkspaceConfig

    link = sb.set_sharing_policy(SharedBrainLink(
        name="notes", path=str(tmp_path / "notes"), identity="github.com/a/notes",
        lifetime_id="unreadable-lifetime",
    ), "automatic")
    config = FakeConfig(tmp_path)
    config.workspaces = {"work": WorkspaceConfig(
        name="work", vault_root="memory-vault/work", shared_brains=[link],
    )}
    (tmp_path / "install" / "state").mkdir(parents=True)
    sb._sharing_revocations_path(config).mkdir()

    sb.apply_persisted_sharing_revocations(config)
    assert config._sharing_recovery_pending is True
    sb.recover_sharing_state(config)
    assert config.workspaces["work"].shared_brains[0].sharing_policy == "review"
    assert config.workspaces["work"].shared_brains[0].sharing_consent is None


def test_distinct_path_journal_writers_merge_without_losing_revocations(tmp_path, monkeypatch) -> None:
    import threading
    import time

    config = FakeConfig(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = sb.save_sharing_revocations
    calls = 0

    def slow_save(config_arg, keys, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(5)
        return original(config_arg, keys, **kwargs)

    monkeypatch.setattr(sb, "save_sharing_revocations", slow_save)
    errors: list[BaseException] = []

    def writer(lifetime: str):
        try:
            sb.merge_sharing_revocations(config, {(lifetime, 1)})
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    first = threading.Thread(target=writer, args=("path-one",))
    first.start()
    assert entered.wait(3)
    second = threading.Thread(target=writer, args=("path-two",))
    second.start()
    time.sleep(0.05)
    release.set()
    first.join(5)
    second.join(5)

    assert not errors
    state = sb.load_sharing_revocations(config)
    assert state.keys == frozenset({("path-one", 1), ("path-two", 1)})


def test_missing_revocation_journal_is_not_treated_as_healthy(tmp_path) -> None:
    config = FakeConfig(tmp_path)
    state = sb.load_sharing_revocations(config)
    assert state.healthy is False
    assert state.fail_closed is True


def test_stale_fence_does_not_demote_a_relinked_grant(tmp_path, monkeypatch) -> None:
    from ciao.config import SharedBrainLink, WorkspaceConfig

    old = sb.set_sharing_policy(SharedBrainLink(
        name="notes", path=str(tmp_path / "notes"), identity="github.com/a/notes",
        lifetime_id="old-lifetime",
    ), "automatic")
    config = FakeConfig(tmp_path)
    config.workspaces = {"work": WorkspaceConfig(
        name="work", vault_root="memory-vault/work", shared_brains=[old],
    )}
    _install_registry(config, config.workspaces["work"])
    fence_path, _grants = sb.write_pending_sharing_fence(config, old)

    new = sb.set_sharing_policy(SharedBrainLink(
        name="notes", path=str(tmp_path / "notes"), identity="github.com/a/notes",
        lifetime_id="new-lifetime",
    ), "automatic")
    config.workspaces["work"].shared_brains = [new]
    _install_registry(config, config.workspaces["work"])

    assert sb.recover_pending_sharing_fences(config) is True
    assert not fence_path.exists()
    assert sb.sharing_consent_active(config.workspaces["work"].shared_brains[0])


def test_fence_recovery_leaves_an_unrelated_link_alone(tmp_path, monkeypatch) -> None:
    from ciao.config import SharedBrainLink, WorkspaceConfig

    monkeypatch.setattr("ciao.workspaces.persist_workspaces", lambda *a, **k: None)

    first = sb.set_sharing_policy(SharedBrainLink(
        name="notes", path=str(tmp_path / "one"), identity="github.com/a/one",
        lifetime_id="one-lifetime",
    ), "automatic")
    second = sb.set_sharing_policy(SharedBrainLink(
        name="other", path=str(tmp_path / "two"), identity="github.com/a/two",
        lifetime_id="two-lifetime",
    ), "automatic")
    config = FakeConfig(tmp_path)
    config.workspaces = {"work": WorkspaceConfig(
        name="work", vault_root="memory-vault/work", shared_brains=[first, second],
    )}
    _install_registry(config, config.workspaces["work"])
    fence_path, _grants = sb.write_pending_sharing_fence(config, first)

    assert sb.recover_pending_sharing_fences(config) is True
    assert not fence_path.exists()
    links = {item.name: item for item in config.workspaces["work"].shared_brains}
    assert links["notes"].sharing_policy == "review"
    assert sb.sharing_consent_active(links["other"])


def _child_merge_journal(state_path: str, lifetime: str, barrier) -> None:
    import multiprocessing
    from types import SimpleNamespace

    from ciao import shared_brain

    barrier.wait(timeout=10)
    config = SimpleNamespace(state_path=Path(state_path))
    shared_brain.merge_sharing_revocations(config, {(lifetime, 1)})


def test_cross_process_journal_writers_do_not_lose_keys(tmp_path) -> None:
    import multiprocessing
    from types import SimpleNamespace

    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    state_path = tmp_path / "state.json"
    processes = [
        context.Process(
            target=_child_merge_journal,
            args=(str(state_path), f"process-{index}", barrier),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    state = sb.load_sharing_revocations(SimpleNamespace(state_path=state_path))
    assert state.keys == frozenset({("process-0", 1), ("process-1", 1)})


def test_fence_after_restore_rotation_is_settled_without_touching_new_link(tmp_path, monkeypatch) -> None:
    from ciao.config import SharedBrainLink, WorkspaceConfig

    old = sb.set_sharing_policy(SharedBrainLink(
        name="notes", path=str(tmp_path / "notes"), identity="github.com/a/notes",
        lifetime_id="pre-restore",
    ), "automatic")
    config = FakeConfig(tmp_path)
    config.workspaces = {"work": WorkspaceConfig(
        name="work", vault_root="memory-vault/work", shared_brains=[old],
    )}
    _install_registry(config, config.workspaces["work"])
    fence_path, _grants = sb.write_pending_sharing_fence(config, old)

    restored = sb.reset_link_for_restore(old)
    config.workspaces["work"].shared_brains = [restored]
    _install_registry(config, config.workspaces["work"])
    monkeypatch.setattr("ciao.workspaces.persist_workspaces", lambda *a, **k: None)

    assert sb.recover_pending_sharing_fences(config) is True
    assert not fence_path.exists()
    assert config.workspaces["work"].shared_brains[0].sharing_policy == "review"


@pytest.mark.asyncio
async def test_update_brain_fences_before_the_origin_read(pair: SimpleNamespace, monkeypatch) -> None:
    from ciao.config import WorkspaceConfig

    link = sb.set_sharing_policy(link_of(pair.clone), "automatic")
    config = FakeConfig(pair.root)
    config.workspaces = {"work": WorkspaceConfig(
        name="work", vault_root="memory-vault/work", shared_brains=[link],
    )}
    _install_registry(config, config.workspaces["work"])
    observed: list[bool] = []

    async def assert_fenced(config_arg, folder, identity):
        observed.append(bool(list((Path(config_arg.state_path).parent / "sharing-pending").glob("*.json"))))
        return sb._result("up_to_date")

    monkeypatch.setattr(sb, "_update_unlocked", assert_fenced)
    result = await sb.update_brain(link, config)

    assert result["outcome"] == "up_to_date"
    assert observed == [True]
    assert list((Path(config.state_path).parent / "sharing-pending").glob("*.json")) == []


@pytest.mark.asyncio
async def test_cancelled_registry_acquisition_is_closed_and_reacquirable(tmp_path) -> None:
    from ciao.workspaces import acquire_workspace_registry

    config = FakeConfig(tmp_path)
    holder = await asyncio.to_thread(acquire_workspace_registry, config, blocking=False)
    task = asyncio.create_task(sb._acquire_registry_claimed(config))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    holder.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    owner = await asyncio.to_thread(acquire_workspace_registry, config, blocking=False)
    owner.close()


def test_killed_observation_releases_claims_for_another_process(pair: SimpleNamespace, tmp_path) -> None:
    from ciao.config import SharingConsent, WorkspaceConfig
    from ciao.workspaces import acquire_workspace_registry

    link = sb.set_sharing_policy(link_of(pair.clone), "automatic")
    config = FakeConfig(tmp_path)
    config.workspaces = {"home": WorkspaceConfig(
        name="home", vault_root="memory-vault/home", shared_brains=[link],
    )}
    _install_registry(config, config.workspaces["home"])
    consent = link.sharing_consent
    assert consent is not None
    ready = tmp_path / "observation-ready"
    script = f"""
import time
from pathlib import Path
from types import SimpleNamespace
from ciao import shared_brain as sb
from ciao.config import SharedBrainLink, SharingConsent, WorkspaceConfig
link = SharedBrainLink(name={link.name!r}, path={link.path!r}, identity={link.identity!r}, lifetime_id={link.lifetime_id!r}, sharing_policy='automatic', sharing_generation={link.sharing_generation}, sharing_consent=SharingConsent(generation={consent.generation}, identity={consent.identity!r}, lifetime_id={consent.lifetime_id!r}, granted_at={consent.granted_at!r}))
workspace = WorkspaceConfig(name='home', vault_root='memory-vault/home', shared_brains=[link])
config = SimpleNamespace(workspace_root=Path({str(config.workspace_root)!r}), state_path=Path({str(config.state_path)!r}), workspaces={{'home': workspace}}, _workspace_registry_bytes=Path({str(config.state_path.parent / 'workspaces.json')!r}).read_bytes(), workspace_names=lambda: [], workspace_vault_root=lambda name: Path('/nonexistent-vault'))
def block(*args, **kwargs):
    Path({str(ready)!r}).write_text('ready')
    time.sleep(60)
    return (1, '')
sb._git = block
sb.brain_state(link, config, 'home')
"""
    env = {**os.environ, "PYTHONPATH": str(Path.cwd())}
    process = subprocess.Popen([sys.executable, "-c", script], env=env, cwd=Path.cwd())
    try:
        for _ in range(200):
            if ready.exists():
                break
            time.sleep(0.02)
        assert ready.exists()
        process.kill()
        process.wait(5)
        owner = acquire_workspace_registry(config, blocking=False)
        owner.close()
        with sb.checkout_claims_nowait([link.path], config):
            pass
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(5)


def test_observation_validates_path_and_config_before_git(tmp_path, monkeypatch) -> None:
    from ciao.config import SharedBrainLink, WorkspaceConfig
    from ciao.workspaces import workspace_registry_claim

    link = SharedBrainLink(
        name="notes", path=str(tmp_path / "missing"), identity="github.com/a/notes",
        lifetime_id="missing-lifetime", sharing_policy="automatic",
    )
    config = FakeConfig(tmp_path)
    config.workspaces = {"work": WorkspaceConfig(
        name="work", vault_root="memory-vault/work", shared_brains=[link],
    )}
    _install_registry(config, config.workspaces["work"])
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(sb, "_git", lambda *args: calls.append(tuple(str(a) for a in args)) or (1, ""))
    with workspace_registry_claim(config) as owner:
        observation = sb.observe_under_fence(config, link, owner)
    assert observation.state["problem"] == "missing"
    assert calls == []


def test_observation_refuses_unsafe_config_before_origin_git(pair: SimpleNamespace, tmp_path, monkeypatch) -> None:
    from ciao.config import WorkspaceConfig
    from ciao.workspaces import workspace_registry_claim

    link = sb.set_sharing_policy(link_of(pair.clone), "automatic")
    git(pair.clone, "config", "filter.x.smudge", "cat")
    config = FakeConfig(tmp_path)
    config.workspaces = {"work": WorkspaceConfig(
        name="work", vault_root="memory-vault/work", shared_brains=[link],
    )}
    _install_registry(config, config.workspaces["work"])
    calls: list[tuple[str, ...]] = []
    original = sb._git

    def record(folder, *args):
        calls.append(tuple(str(a) for a in args))
        return original(folder, *args)

    monkeypatch.setattr(sb, "_git", record)
    with workspace_registry_claim(config) as owner:
        observation = sb.observe_under_fence(config, link, owner)
    assert observation.state["problem"] == "unsafe_config"
    assert calls == [("config", "--list", "--show-scope")]
