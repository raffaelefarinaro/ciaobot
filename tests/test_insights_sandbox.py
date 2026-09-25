"""Tests for the insights sandbox helpers (``scripts/insights-sandbox/``).

The harness lets a full agent really write, so the only thing standing between
a pilot run and the live workspace is these two modules. The tests are
therefore mostly about refusals: a path check that passes is a claim about the
operator's real files, and a ``prepare_clone`` step that silently no-ops is a
step the harness's own README promises.

The modules are loaded by path rather than imported: ``scripts/insights-sandbox``
is a directory with a hyphen, not a package.
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MODULE_PATH = _REPO_ROOT / "scripts" / "insights-sandbox" / "sandbox.py"
_DRIVER_PATH = _REPO_ROOT / "scripts" / "insights-sandbox" / "run.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: `@dataclass` resolves its string annotations
    # through `sys.modules[cls.__module__]`, which is None until the loader has
    # put the module there.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_sandbox():
    return _load("insights_sandbox_helpers", _MODULE_PATH)


def _load_run():
    """The driver, loaded the same way.

    Importing it is cheap and side-effect free: it inserts its own directory on
    ``sys.path`` and defines functions, and it starts nothing until ``main``.
    The containment helpers live here rather than in ``sandbox.py`` because
    they need ``CiaoConfig``, and the driver's own module-level imports are
    what ``--help`` already exercises.
    """
    return _load("insights_sandbox_driver", _DRIVER_PATH)


sandbox = _load_sandbox()
# The driver does `from sandbox import SandboxError`, so it is registered under
# that name here rather than loaded as a second copy: with one module object the
# driver's exceptions and these tests' `pytest.raises` are the same class, which
# is what a real run gets.
sys.modules.setdefault("sandbox", sandbox)
run = _load_run()


def _git(clone: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(clone), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ── path safety ──────────────────────────────────────────────────────────


def test_assert_sandbox_path_refuses_live_and_outside(tmp_path: Path) -> None:
    root = tmp_path / "sandbox"
    live = tmp_path / "live"
    (root / "run").mkdir(parents=True)
    live.mkdir()

    # The happy path: a clone directory inside the run directory.
    clone = sandbox.assert_sandbox_path(
        root / "run" / "base", sandbox_root=root, live=live
    )
    assert clone == (root / "run" / "base").resolve()

    # The live workspace itself, and a path inside it.
    with pytest.raises(sandbox.SandboxError):
        sandbox.assert_sandbox_path(live, sandbox_root=root, live=live)
    with pytest.raises(sandbox.SandboxError):
        sandbox.assert_sandbox_path(
            live / "memory-vault", sandbox_root=root, live=live
        )

    # Outside the sandbox root entirely.
    with pytest.raises(sandbox.SandboxError):
        sandbox.assert_sandbox_path(
            tmp_path / "elsewhere" / "base", sandbox_root=root, live=live
        )

    # The sandbox root is not itself a clone: it holds the report and the
    # clones, and a "clone" here would be the run directory the diffs live in.
    with pytest.raises(sandbox.SandboxError):
        sandbox.assert_sandbox_path(root, sandbox_root=root, live=live)

    # A sandbox root that contains the live path, where the checked path is an
    # ancestor of it: a write into that "clone" would escape upward into the
    # real files, so the containment runs the other way round too.
    inner_live = live / "inner"
    inner_live.mkdir()
    with pytest.raises(sandbox.SandboxError):
        sandbox.assert_sandbox_path(live, sandbox_root=tmp_path, live=inner_live)
    # ... while a sibling clone in that same root is still fine.
    assert sandbox.assert_sandbox_path(
        root / "run" / "base", sandbox_root=tmp_path, live=inner_live
    ) == (root / "run" / "base").resolve()


def test_assert_contained_refuses_anything_outside_the_clone(tmp_path: Path) -> None:
    """Every path an arm is handed has to land inside its clone.

    The complement of ``assert_sandbox_path``: that one proves the clone is in
    the sandbox tree, this one proves the paths the arms were given are in the
    clone. Absolute vault roots, an external vault and a project doc outside
    the workspace are all supported ``CiaoConfig`` settings, and every one of
    them is an instruction to write the original files.
    """
    clone = (tmp_path / "clone").resolve()
    outside = (tmp_path / "outside-vault").resolve()
    clone.mkdir()
    outside.mkdir()
    os.symlink(outside, clone / "escape", target_is_directory=True)

    # Inside: the clone itself (a workspace that is its own vault is a
    # supported layout) and anything under it.
    sandbox.assert_contained(
        [clone, clone / "memory-vault", clone / "memory-vault" / "work" / "p.md"],
        clone,
    )

    # Outside: a sibling directory, a symlink out of the clone, and a `..` that
    # walks out of it. Resolution happens first, so none of the three is
    # smuggled past by looking contained.
    for escaping in (outside, clone / "escape", clone / ".."):
        with pytest.raises(sandbox.SandboxError) as excinfo:
            sandbox.assert_contained([clone, escaping], clone)
        assert "outside the clone" in str(excinfo.value)

    # The first offending path is the one named, not merely the first in some
    # other order: the point of raising on the first is that the message is
    # about the path that actually stopped the run.
    with pytest.raises(sandbox.SandboxError) as excinfo:
        sandbox.assert_contained([clone, outside, Path("/etc")], clone)
    assert str(outside) in str(excinfo.value)
    assert "/etc" not in str(excinfo.value)


def test_clone_config_is_bound_to_the_clone_not_the_ambient_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The config the containment check inspects is the one the arm will use.

    ``CiaoConfig.from_env`` falls back to the LaunchAgent's (live) workspace
    when ``CIAO_WORKSPACE`` is missing, so a config built from the ambient
    environment would make every containment check a check on the wrong paths.
    """
    monkeypatch.setenv("CIAO_WORKSPACE", "/somewhere/live")
    clone = (tmp_path / "clone").resolve()
    (clone / ".runtime").mkdir(parents=True)
    config = run.clone_config(clone)
    assert config.workspace_root == clone
    assert config.state_path.parent == clone / ".runtime"


def _requires_clonefile(tmp_path: Path) -> None:
    """``cp -c`` needs a copy-on-write filesystem; the harness cannot run without it.

    The copy is not incidental here: this is the test that proves a copy of a
    *linked* worktree cannot be prepared, and a filesystem that cannot clone
    files at all has no way to produce that shape to check.
    """
    src = tmp_path / "cp-src"
    src.write_text("x", encoding="utf-8")
    probe = subprocess.run(
        ["cp", "-c", str(src), str(tmp_path / "cp-dst")], capture_output=True
    )
    if probe.returncode != 0:
        pytest.skip(
            "`cp -c` is unavailable here: "
            + probe.stderr.decode("utf-8", "replace").strip()
        )


def test_clone_workspace_refuses_a_linked_worktree(tmp_path: Path) -> None:
    """A copy of a linked worktree is refused before any git mutation of the source.

    A linked worktree's ``.git`` is a *text file* naming the source
    repository's absolute common git directory, and ``cp`` copies it verbatim:
    the copy is not a repository of its own, so ``git -C <clone> remote remove``
    runs against the source -- stripping the operator's remote -- and every
    per-chat ``commit`` writes into the source's metadata. The source must come
    out of this exactly as it went in, which is what the test asserts: the
    remote, and every ref.
    """
    _requires_clonefile(tmp_path)
    source = tmp_path / "source"
    _git(tmp_path, "init", str(source), "-q")
    _git(source, "config", "user.email", "t@example.invalid")
    _git(source, "config", "user.name", "T")
    (source / "a.md").write_text("# a\n", encoding="utf-8")
    _git(source, "add", "-A")
    _git(source, "commit", "-q", "-m", "init")
    _git(source, "remote", "add", "origin", "https://example.invalid/x.git")
    live = tmp_path / "live-worktree"
    _git(source, "worktree", "add", "-q", str(live), "-b", "sandbox")
    refs = "--format=%(refname) %(objectname)"
    remotes_before = _git(source, "remote")
    refs_before = _git(source, "for-each-ref", refs)

    dest = tmp_path / "sandbox" / "run" / "base"
    with pytest.raises(sandbox.SandboxError) as excinfo:
        sandbox.clone_workspace(live, dest)
    assert "linked git worktree" in str(excinfo.value)
    assert "main repository" in str(excinfo.value)
    # A refused copy is a hazard, not a clone: leaving it would make the next
    # run refuse on "already exists" and hide why the first one stopped.
    assert not dest.exists()

    assert _git(source, "remote") == remotes_before == "origin"
    assert _git(source, "for-each-ref", refs) == refs_before


def test_an_ordinary_clone_is_a_repository_of_its_own(tmp_path: Path) -> None:
    """The same check passes for the normal shape, so it is not a blanket refusal.

    ``git rev-parse --git-common-dir`` prints ``.git`` -- relative to the
    repository it was asked about -- for an ordinary clone, and only resolving
    it against the copy is what lets this check see two paths that are inside.
    """
    _requires_clonefile(tmp_path)
    source = tmp_path / "source"
    _git(tmp_path, "init", str(source), "-q")
    dest = tmp_path / "sandbox" / "run" / "base"
    sandbox.clone_workspace(source, dest)
    assert dest.exists()
    git_dir, common = sandbox.git_dirs(dest)
    assert git_dir == common == (dest / ".git").resolve()


# ── port selection ───────────────────────────────────────────────────────


def test_pick_port_zero_returns_free_port() -> None:
    """The default: a port the harness's own server can actually bind.

    A second Ciaobot instance is enough to make a fixed port wrong -- it binds
    ``*:port``, answers the harness's login with a 401, and the pilot dies
    before the first chat. So the default is not "a port nobody uses", it is
    "a port the kernel just gave us".
    """
    port = sandbox.pick_port(0)
    assert 1024 < port < 65536
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", port))


def test_pick_port_refuses_busy_port() -> None:
    """A port somebody is listening on is refused, not adopted.

    The stranger's server is left running, so adopting its port would put the
    harness's agent token behind a Ciaobot instance that never issued it: the
    login 401s, and the harness's own server sits on a port it cannot reach.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        with pytest.raises(sandbox.SandboxError) as excinfo:
            sandbox.pick_port(port)
    assert str(port) in str(excinfo.value)

    # A free explicit port is the operator's to have, and is returned as asked.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    assert sandbox.pick_port(free) == free

    # A negative port is not a port.
    with pytest.raises(sandbox.SandboxError):
        sandbox.pick_port(-1)


# ── the arms' path containment ───────────────────────────────────────────


@pytest.fixture
def escaping_clone(tmp_path: Path) -> tuple[Path, Path, list[object]]:
    """A clone whose vault and workspace registry point outside it.

    Resolved, because ``CiaoConfig`` refuses an absolute ``vault_root`` whose
    own path contains a symlink, and pytest's ``tmp_path`` may sit behind one
    on macOS. The external vault is created first so it exists: a configured
    vault that is not there yet is still an external path.
    """
    clone = (tmp_path / "clone").resolve()
    outside = (tmp_path / "outside-vault").resolve()
    (clone / ".runtime").mkdir(parents=True)
    outside.mkdir()
    _write_json(
        clone / ".runtime" / "workspaces.json",
        [{"name": "work", "vault_root": "memory-vault/work"}],
    )
    rows = [
        run.Row(
            chat_id="chat-1",
            workspace="work",
            provider="claude",
            effective_provider="claude",
            model="opus",
            archive_rel=Path("Logs/Chats/chat-1/claude/a.md"),
        )
    ]
    return clone, outside, rows


def _config(clone: Path, **env: str):
    from ciao.config import CiaoConfig

    return CiaoConfig.from_env(
        {
            "CIAO_WORKSPACE": str(clone),
            "CIAO_RUNTIME_ROOT": str(clone / ".runtime"),
            "PWA_AUTH_TOKEN": "sandbox",
            **env,
        }
    )


def test_absolute_vault_root_is_refused_not_written(
    escaping_clone: tuple[Path, Path, list[object]],
) -> None:
    """An absolute ``CIAO_VAULT_ROOT`` stops the run, and nothing is written.

    Absolute vault roots are a supported configuration -- ``CiaoConfig``
    preserves them on purpose -- so an operator can have one. Cloned as-is the
    one-shot arm and the agent both resolve the *original* vault, and the
    harness cannot rebase the value: it is this env, not a file the prep can
    rewrite. Failing closed is the only safe answer, and the test asserts the
    external vault is untouched afterwards.
    """
    clone, outside, rows = escaping_clone
    outside.mkdir(exist_ok=True)
    (outside / "sentinel.md").write_text("# do not touch\n", encoding="utf-8")
    before = sorted(p.name for p in outside.iterdir())

    # The normal install passes, so the refusal below is about the path and not
    # about the check being stricter than every configuration.
    run.assert_clone_containment(_config(clone), clone, rows)

    with pytest.raises(sandbox.SandboxError) as excinfo:
        run.assert_clone_containment(
            _config(clone, CIAO_VAULT_ROOT=str(outside)), clone, rows
        )
    assert str(outside) in str(excinfo.value)

    # The refusal happened before any arm ran, so the external vault is exactly
    # as it was.
    assert sorted(p.name for p in outside.iterdir()) == before
    assert (outside / "sentinel.md").read_text(encoding="utf-8") == "# do not touch\n"


def test_absolute_workspace_vault_root_is_refused(
    escaping_clone: tuple[Path, Path, list[object]],
) -> None:
    """A per-workspace ``vault_root`` outside the clone is refused too.

    This is the one ``prepare_clone`` most looks like it covers: the value
    lives in ``.runtime/workspaces.json`` inside the clone, and the prep already
    rewrites that file to clear the integration switches. It preserves
    ``vault_root``, because for a real install that value is the operator's
    data location and rewriting it would be far worse than refusing to run.
    """
    clone, outside, rows = escaping_clone
    _write_json(
        clone / ".runtime" / "workspaces.json",
        [{"name": "work", "vault_root": str(outside)}],
    )
    config = _config(clone)
    # The registry value is preserved, not rebased: that is the whole point.
    assert config.workspace_vault_root("work") == outside
    with pytest.raises(sandbox.SandboxError) as excinfo:
        run.assert_clone_containment(config, clone, rows)
    assert str(outside) in str(excinfo.value)
    assert list(outside.iterdir()) == []


def test_absolute_project_doc_is_refused_and_never_joined(
    escaping_clone: tuple[Path, Path, list[object]],
) -> None:
    """An absolute ``vault_doc_path`` is refused, and not joined onto the clone.

    ``agent_clone / row.doc`` looks like a rebase and is not: ``Path`` ignores
    the left operand when the right one is absolute, so an external doc would
    reach the agent's prompt unchanged. The check is what refuses it, which is
    only true if the join is not silently doing the work.
    """
    clone, outside, rows = escaping_clone
    external_doc = outside / "Projects" / "Ada.md"
    rows[0].doc = str(external_doc)  # type: ignore[attr-defined]

    with pytest.raises(sandbox.SandboxError) as excinfo:
        run.assert_clone_containment(_config(clone), clone, rows)
    assert str(external_doc.resolve()) in str(excinfo.value)

    # The doc is passed through, not joined: `clone / external_doc` would return
    # the external path, and a relative doc is what actually gets rebased.
    assert run.clone_doc(clone, str(external_doc)) == external_doc
    assert run.clone_doc(clone, "memory-vault/work/Ada.md") == (
        clone / "memory-vault/work/Ada.md"
    )

    # A relative doc that stays inside the clone is fine, and is rebased.
    rows[0].doc = "memory-vault/work/Ada.md"  # type: ignore[attr-defined]
    run.assert_clone_containment(_config(clone), clone, rows)

    # `docs=[]` means "no docs yet", which is the pre-boot call: there is no
    # project map before the server has answered. It has to stay distinct from
    # the default -- a `docs or ...` fallback would read the empty list as
    # unset and check the rows' docs, which are not filled in yet.
    rows[0].doc = str(external_doc)  # type: ignore[attr-defined]
    run.assert_clone_containment(_config(clone), clone, rows, docs=[])
    with pytest.raises(sandbox.SandboxError):
        run.assert_clone_containment(_config(clone), clone, rows, docs=None)


def test_every_selected_workspace_root_is_checked(
    escaping_clone: tuple[Path, Path, list[object]],
) -> None:
    """A workspace nothing in the selection needs is still resolved and refused.

    Both arms iterate workspaces, not chats, so a workspace whose agent root
    escapes would be resolved during a run even if no selected chat belongs to
    it. Checking only the selected workspaces' *chats* would miss that.
    """
    clone, outside, rows = escaping_clone
    rows.append(  # type: ignore[union-attr]
        run.Row(
            chat_id="chat-2",
            workspace="other",
            provider="claude",
            effective_provider="claude",
            model="opus",
            archive_rel=Path("Logs/Chats/chat-2/claude/b.md"),
        )
    )
    _write_json(
        clone / ".runtime" / "workspaces.json",
        [
            {"name": "work", "vault_root": "memory-vault/work"},
            {"name": "other", "vault_root": str(outside)},
        ],
    )
    with pytest.raises(sandbox.SandboxError) as excinfo:
        run.assert_clone_containment(_config(clone), clone, rows)
    assert str(outside) in str(excinfo.value)


def test_an_archive_outside_the_clone_is_refused(
    escaping_clone: tuple[Path, Path, list[object]],
) -> None:
    """A selected archive that is not in the clone is refused.

    The selection is built from the live workspace, so a row pointing outside
    the clone is either a stale cache entry or a tampered one. Either way, both
    arms would read -- and the one-shot arm would then append to -- a file that
    is not the sandbox's.
    """
    clone, outside, rows = escaping_clone
    rows[0].archive_rel = outside / "elsewhere.md"  # type: ignore[attr-defined]
    with pytest.raises(sandbox.SandboxError) as excinfo:
        run.assert_clone_containment(_config(clone), clone, rows)
    assert str((outside / "elsewhere.md").resolve()) in str(excinfo.value)


# ── the effective configuration the child will build ─────────────────────


def _dotenv_clone(tmp_path: Path) -> tuple[Path, Path, list[object]]:
    """A minimal clone whose ``.env`` names a vault outside it, and that vault.

    ``PWA_AUTH_TOKEN`` is there because a real ``.env`` has one: without it the
    config load manufactures a session secret, which is a write.
    """
    clone = (tmp_path / "clone").resolve()
    external = (tmp_path / "external-vault").resolve()
    (clone / ".runtime").mkdir(parents=True)
    external.mkdir()
    _write_json(
        clone / ".runtime" / "workspaces.json",
        [{"name": "work", "vault_root": "memory-vault/work"}],
    )
    (clone / ".env").write_text(
        f"CIAO_VAULT_ROOT={external}\nPWA_AUTH_TOKEN=y\n", encoding="utf-8"
    )
    rows = [
        run.Row(
            chat_id="chat-1",
            workspace="work",
            provider="claude",
            effective_provider="claude",
            model="opus",
            archive_rel=Path("Logs/Chats/chat-1/claude/a.md"),
        )
    ]
    return clone, external, rows


def test_probe_reads_the_configuration_the_server_will_build(tmp_path: Path) -> None:
    """The probe sees what ``ciao run`` sees, ``.env`` included.

    This is the whole reason the probe exists. A config built from an explicit
    env dict -- what the one-shot arm and the pre-boot check use -- never reads
    ``<clone>/.env``, and the spawned server calls ``from_env()`` with no
    arguments, which does. So for a clone whose ``.env`` sets an absolute
    ``CIAO_VAULT_ROOT`` the two disagree, and the child is the one that writes.
    The probe is run for real here, on a minimal workspace, so the disagreement
    is a measurement rather than an assumption.
    """
    clone, external, _ = _dotenv_clone(tmp_path)

    effective = run.probe_effective_config(clone, run.clone_env(clone), ["work"])
    assert effective["vault_root"] == str(external)
    assert effective["workspace_root"] == str(clone)
    assert effective["runtime_root"] == str(clone / ".runtime")
    assert effective["sync_root"] == str(clone)
    assert effective["workspaces"]["work"]["agent_root"] == str(clone)
    assert effective["workspaces"]["work"]["workspace_vault_root"] == str(
        clone / "memory-vault" / "work"
    )

    # A shape the check cannot read is a refusal, never an empty list: a probe
    # answer that is missing a key is a configuration nobody has validated.
    with pytest.raises(sandbox.SandboxError):
        run.assert_effective_containment({"vault_root": str(clone)}, clone)
    with pytest.raises(sandbox.SandboxError):
        run.assert_effective_containment(
            {**effective, "workspaces": {"work": {"agent_root": str(clone)}}}, clone
        )

    # The clean case is inside, so the check is not simply "refuse everything".
    (clone / ".env").write_text("PWA_AUTH_TOKEN=y\n", encoding="utf-8")
    clean = run.probe_effective_config(clone, run.clone_env(clone), ["work"])
    assert clean["vault_root"] == str(clone / "memory-vault")
    run.assert_effective_containment(clean, clone)


def test_start_server_refuses_external_vault_from_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live vault in the clone's ``.env`` stops the run before anything spawns.

    The server loads the clone's ``.env``, so ``CIAO_VAULT_ROOT`` naming the
    live vault is a write to the operator's files from the moment it boots --
    including the startup index refresh, which happens before any chat. The
    probe answers the question the preflight could not, and the refusal has to
    land before ``Popen``: once the process exists, a full agent with Bash is
    running against the clone.
    """
    clone, external, rows = _dotenv_clone(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    spawned: list[list[str]] = []
    real_popen = subprocess.Popen

    def recording_popen(command, *args, **kwargs):  # type: ignore[no-untyped-def]
        spawned.append([str(part) for part in command])
        return real_popen(command, *args, **kwargs)

    # `subprocess.run` resolves `Popen` from the module at call time, so
    # recording rather than replacing is what lets the probe run for real
    # while still proving the *server* was never spawned.
    monkeypatch.setattr(subprocess, "Popen", recording_popen)

    with pytest.raises(sandbox.SandboxError) as excinfo:
        run.start_server(clone, run_dir, 0, rows)
    assert str(external) in str(excinfo.value)

    server_spawns = [cmd for cmd in spawned if "ciao.cli" in cmd]
    assert not server_spawns, f"the server must not be spawned: {server_spawns}"
    assert not (run_dir / "agent-server.log").exists()
    # The preflight's own view -- an explicit env dict, which never reads the
    # ``.env`` -- is the one that would have passed.
    run.assert_clone_containment(_config(clone), clone, rows, docs=[])
    assert list(external.iterdir()) == [], "the external vault must be untouched"


def test_the_vault_repository_loses_its_remote_too(tmp_path: Path) -> None:
    """The repo the server actually pushes is the vault's, not only the install's.

    ``_branch_backup_loop`` pushes ``local_session.sync_root(config)``, which is
    the repository containing the *vault*. When the vault lives outside the
    workspace in a repository of its own, that is a second remote the install
    root's cleanup never sees -- and it is the agent arm's server that pushes,
    every 30 seconds.
    """
    clone = tmp_path / "clone"
    clone.mkdir()
    vault = clone / "memory-vault"
    _git(tmp_path, "init", str(vault), "-q")
    _git(vault, "config", "user.email", "t@example.invalid")
    _git(vault, "config", "user.name", "T")
    _git(vault, "remote", "add", "origin", "https://example.invalid/vault.git")

    run.neutralise_sync_root({"sync_root": str(vault)}, clone)
    assert _git(vault, "remote") == ""

    # An outside sync root is refused, and its remote is left alone: running
    # `git remote remove` against the operator's own repository would be the
    # damage this check exists to prevent, not prevent.
    live_vault = tmp_path / "live-vault"
    _git(tmp_path, "init", str(live_vault), "-q")
    _git(live_vault, "remote", "add", "origin", "https://example.invalid/live.git")
    with pytest.raises(sandbox.SandboxError) as excinfo:
        run.neutralise_sync_root({"sync_root": str(live_vault)}, clone)
    assert "outside the clone" in str(excinfo.value)
    assert _git(live_vault, "remote") == "origin"


# ── clone preparation ────────────────────────────────────────────────────


@pytest.fixture
def fake_workspace(tmp_path: Path) -> tuple[Path, Path]:
    """A clone-shaped workspace, plus the packaged stock schedule file."""
    clone = tmp_path / "live"
    runtime = clone / ".runtime"
    runtime.mkdir(parents=True)
    _git(clone.parent, "init", str(clone), "-q")
    _git(clone, "config", "user.email", "t@example.invalid")
    _git(clone, "config", "user.name", "T")
    _git(clone, "remote", "add", "origin", "https://example.invalid/x.git")

    _write_json(
        runtime / "schedules.json",
        {"schedules": [{"schedule_id": "u1", "enabled": True, "title": "nightly"}]},
    )
    _write_json(
        runtime / "system_schedules_state.json",
        {"schedules": {"system-error-triage": {"enabled": True, "last_dispatched_at": "x"}}},
    )
    _write_json(
        runtime / "workspaces.json",
        [
            {
                "name": "work",
                "allowed_mcp_servers": ["notion"],
                "claude_ai_mcps": True,
                "gws_profile": "work-gws",
                "vault_root": "work/memory-vault",
            }
        ],
    )
    _write_json(runtime / "push_subscriptions.json", {"subs": ["endpoint"]})
    (runtime / "background").mkdir()
    (runtime / "background" / "orphan.json").write_text("{}", encoding="utf-8")
    (clone / ".mcp.json").write_text("{}", encoding="utf-8")
    (clone / "sub").mkdir()
    (clone / "sub" / ".mcp.json").write_text("{}", encoding="utf-8")
    (clone / ".env").write_text(
        "# keep me\nCIAO_WORKSPACE=.\nNOTION_TOKEN=x\nPWA_AUTH_TOKEN=y\n\n",
        encoding="utf-8",
    )
    stock = tmp_path / "stock.json"
    _write_json(stock, {"schedules": [{"schedule_id": "system-memory-curation"}]})
    return clone, stock


def test_prepare_clone_neutralises_side_effects(fake_workspace: tuple[Path, Path]) -> None:
    clone, stock = fake_workspace
    notes = sandbox.prepare_clone(
        clone, stock_schedules=stock, disable_insights=True
    )
    assert notes, "prepare_clone must report what it did"

    # The push loop and the phone notifications.
    assert _git(clone, "remote") == ""
    assert not (clone / ".runtime" / "push_subscriptions.json").exists()

    # Both schedule sources, including a system id the state file had never
    # seen: a created entry that stays enabled would fire on the next tick.
    user = json.loads((clone / ".runtime" / "schedules.json").read_text())
    assert all(row["enabled"] is False for row in user["schedules"])
    system = json.loads(
        (clone / ".runtime" / "system_schedules_state.json").read_text()
    )["schedules"]
    assert system["system-error-triage"]["enabled"] is False
    # An existing entry keeps its other fields.
    assert system["system-error-triage"]["last_dispatched_at"] == "x"
    assert "system-memory-curation" in system
    assert system["system-memory-curation"]["enabled"] is False
    assert "system-memory-curation@work" in system
    assert system["system-memory-curation@work"]["enabled"] is False

    # The startup triage chat and any woken background task.
    triage = json.loads((clone / ".runtime" / "startup_triage.json").read_text())
    assert triage["last_dispatched_at"]
    assert list((clone / ".runtime" / "background").iterdir()) == []

    # The archive pipeline must not run under the harness's own chats.
    settings = json.loads((clone / ".runtime" / "app_settings.json").read_text())
    assert settings["insights_enabled"] is False
    assert settings["trajectories_enabled"] is False

    # Integrations: no MCP server, no credential, no workspace profile.
    assert list(clone.rglob(".mcp.json")) == []
    env = (clone / ".env").read_text()
    assert "NOTION_TOKEN" not in env
    assert "CIAO_WORKSPACE=." in env
    assert "PWA_AUTH_TOKEN=y" in env
    assert "# keep me" in env
    workspace = json.loads((clone / ".runtime" / "workspaces.json").read_text())[0]
    assert workspace["allowed_mcp_servers"] == []
    assert workspace["claude_ai_mcps"] is False
    assert workspace["gws_profile"] == ""


def test_prepare_clone_keeps_insights_when_not_disabled(
    fake_workspace: tuple[Path, Path],
) -> None:
    """The one-shot arm needs the pipeline on; only the agent clone is muted."""
    clone, stock = fake_workspace
    sandbox.prepare_clone(clone, stock_schedules=stock, disable_insights=False)
    assert not (clone / ".runtime" / "app_settings.json").exists()


def test_prepare_clone_drops_path_bearing_env_keys(
    fake_workspace: tuple[Path, Path],
) -> None:
    """A retained key may not carry a path that points outside the clone.

    Keeping ``CIAO_*``/``PWA_*`` is not enough on its own: ``CIAO_VAULT_ROOT``
    is a key the filter keeps, and in a real install it holds the *live*
    vault's path. The spawned server loads the clone's ``.env`` and writes
    there, which is the escape the effective-config probe refuses a run over --
    but the clone should not carry the value in the first place, so the drop
    happens here and the dropped keys are named in ``prep.log``, since a
    changed install is what the next question is about.
    """
    clone, stock = fake_workspace
    live_vault = clone.parent / "live-vault"
    live_vault.mkdir()
    (clone / ".env").write_text(
        "# keep me\n"
        "CIAO_WORKSPACE=.\n"
        "PWA_AUTH_TOKEN=y\n"
        "NOTION_TOKEN=x\n"
        f"CIAO_VAULT_ROOT={live_vault}\n"
        "PWA_STATIC_DIR=~/public\n"
        "CIAO_RUNTIME_ROOT=../elsewhere\n"
        'CIAO_MODEL_DIR="/absolute/quoted"\n',
        encoding="utf-8",
    )

    notes = sandbox.prepare_clone(clone, stock_schedules=stock, disable_insights=False)
    env = (clone / ".env").read_text(encoding="utf-8")
    for dropped in (
        "CIAO_VAULT_ROOT",
        "PWA_STATIC_DIR",
        "CIAO_RUNTIME_ROOT",
        "CIAO_MODEL_DIR",
    ):
        assert dropped not in env
    # ... and everything that is a setting rather than a location survives, so
    # the filter is not simply "drop every retained key".
    assert "CIAO_WORKSPACE=." in env
    assert "PWA_AUTH_TOKEN=y" in env
    assert "# keep me" in env
    assert "NOTION_TOKEN" not in env
    assert any(
        "CIAO_VAULT_ROOT" in note and "PWA_STATIC_DIR" in note for note in notes
    ), f"prep.log must name the dropped keys: {notes}"


# ── archives and commits ─────────────────────────────────────────────────


def test_strip_archives_removes_insights_sections(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    archive = clone / "Logs" / "Chats" / "chat-1" / "claude" / "a.md"
    archive.parent.mkdir(parents=True)
    archive.write_text(
        "# Chat\n\nthe transcript.\n\n## Session insights\n\n- a fact [memory]\n",
        encoding="utf-8",
    )
    plain = clone / "Logs" / "Chats" / "chat-2" / "claude" / "b.md"
    plain.parent.mkdir(parents=True)
    plain.write_text("# Chat\n\nno insights here.\n", encoding="utf-8")

    changed = sandbox.strip_archives(
        clone, ["Logs/Chats/chat-1/claude/a.md", "Logs/Chats/chat-2/claude/b.md"]
    )

    assert changed == 1
    assert "Session insights" not in archive.read_text()
    assert "the transcript." in archive.read_text()
    # A body with no appended section is returned unchanged, not rewritten.
    assert plain.read_text() == "# Chat\n\nno insights here.\n"


def test_commit_snapshot_and_diff_summary_classify(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    _git(tmp_path, "init", str(clone), "-q")
    _git(clone, "config", "user.email", "t@example.invalid")
    _git(clone, "config", "user.name", "T")
    _git(clone, "commit", "--allow-empty", "-q", "-m", "harness: baseline")
    base_sha = _git(clone, "rev-parse", "HEAD")

    vault = clone / "memory-vault" / "work" / "People"
    vault.mkdir(parents=True)
    (vault / "ada.md").write_text("# Ada\n", encoding="utf-8")
    guide = clone / "work" / "AGENTS.md"
    guide.parent.mkdir(parents=True)
    guide.write_text("# guide\n", encoding="utf-8")
    proposals = clone / "memory-vault" / "work" / "Workspace" / "Memory-Proposals.md"
    proposals.parent.mkdir(parents=True)
    proposals.write_text("# Proposals\n", encoding="utf-8")
    logs = clone / "Logs" / "Chats" / "chat-1" / "claude" / "a.md"
    logs.parent.mkdir(parents=True)
    logs.write_text("# Chat\n", encoding="utf-8")
    sandbox.commit_snapshot(clone, "harness: baseline")
    prepared = _git(clone, "rev-parse", "HEAD")

    # The arm's one chat: a new note, an edited note, a 2-bullet queue append,
    # a transcript rewrite and a guide edit.
    (vault / "ada.md").write_text("# Ada\n\nlikes tea.\n", encoding="utf-8")
    (vault / "grace.md").write_text("# Grace\n", encoding="utf-8")
    proposals.write_text(
        "# Proposals\n\n- maybe a thing [review]\n- maybe another [review]\n",
        encoding="utf-8",
    )
    logs.write_text("# Chat\n\nmore.\n", encoding="utf-8")
    guide.write_text("# guide\n\nexpanded.\n", encoding="utf-8")
    sha = sandbox.commit_snapshot(clone, "agent chat-1")

    summary = sandbox.diff_summary(clone, prepared, sha)
    assert set(summary) == {"added", "modified", "deleted", "by_class", "queued"}
    assert summary["added"] == [
        "memory-vault/work/People/grace.md",
    ]
    # The transcript is not in `modified`: every a/m/d number the report prints
    # is the length of one of these lists, so leaving `Logs/` in them counted a
    # server-written file as the arm's work while the report claimed logs were
    # excluded. Dropped at the source, not only from `by_class`.
    assert summary["modified"] == [
        "memory-vault/work/People/ada.md",
        "memory-vault/work/Workspace/Memory-Proposals.md",
        "work/AGENTS.md",
    ]
    assert summary["deleted"] == []
    assert "logs" not in summary["by_class"]
    assert summary["by_class"] == {"vault": 2, "proposals": 1, "guide": 1}
    assert summary["queued"] == 2
    # The baseline commit is its own sha, and a clone with nothing new is
    # empty rather than an error.
    assert base_sha != prepared
    assert sandbox.diff_summary(clone, sha, sha)["by_class"] == {}


def test_classify_paths() -> None:
    assert sandbox.classify("memory-vault/work/Workspace/Memory-Proposals.md") == "proposals"
    assert sandbox.classify("Workspace/Memory-Proposals.md") == "proposals"
    assert sandbox.classify("work/AGENTS.md") == "guide"
    assert sandbox.classify("CLAUDE.md") == "guide"
    assert sandbox.classify("memory-vault/work/MEMORY.md") == "guide"
    assert sandbox.classify("memory-vault/work/People/ada.md") == "vault"
    assert sandbox.classify("work/memory-vault/work/Notes/x.md") == "vault"
    assert sandbox.classify("Logs/Chats/chat-1/claude/a.md") == "logs"
    # A vault path under Logs/ is transcript evidence, not vault output.
    assert sandbox.classify("memory-vault/Logs/x.md") == "logs"
    assert sandbox.classify("pyproject.toml") == "other"
    assert sandbox.classify("web/src/main.tsx") == "other"


# ── setup is not chat 1 ──────────────────────────────────────────────────


def test_boot_and_project_setup_are_absent_from_chat_one(tmp_path: Path) -> None:
    """Chat 1's diff is the chat's, not the server's boot and the harness's projects.

    The attribution is per chat, and it is only worth anything if nothing else
    is in the window. Booting the server writes to the clone before any chat
    exists (a regenerated ``INDEX.md``), and the harness's own per-workspace
    projects are created before the first chat, so with the pre-boot baseline
    as chat 1's ``previous_sha`` both landed in chat 1's counts: the one number
    in the report a reader cannot explain.

    The two setup commits are what fix it, so this reproduces the exact commit
    sequence the driver runs and asserts the setup paths are gone from chat 1 --
    and that they *were* there before, or the assertion proves nothing.
    """
    clone = tmp_path / "clone"
    clone.mkdir()
    _git(tmp_path, "init", str(clone), "-q")
    _git(clone, "config", "user.email", "t@example.invalid")
    _git(clone, "config", "user.name", "T")
    baseline = sandbox.commit_snapshot(clone, "harness: baseline")

    # Boot: the server regenerates the vault index.
    index = clone / "memory-vault" / "INDEX.md"
    index.parent.mkdir(parents=True)
    index.write_text("# Index\n", encoding="utf-8")
    boot_sha = sandbox.commit_snapshot(clone, "harness: server boot")
    # Setup: the harness creates its "Insights sandbox" project per workspace.
    _write_json(
        clone / ".runtime" / "web_projects.json",
        {"projects": {"p1": {"name": "Insights sandbox", "workspace": "work"}}},
    )
    setup_sha = sandbox.commit_snapshot(clone, "harness: project setup")

    # Chat 1: the agent writes one vault note.
    note = clone / "memory-vault" / "work" / "People" / "ada.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Ada\n\nlikes tea.\n", encoding="utf-8")
    chat_sha = sandbox.commit_snapshot(clone, "agent chat-1")

    chat_one = sandbox.diff_summary(clone, setup_sha, chat_sha)
    changed = [
        *chat_one["added"],
        *chat_one["modified"],
        *chat_one["deleted"],
    ]
    assert changed == ["memory-vault/work/People/ada.md"]
    assert chat_one["by_class"] == {"vault": 1}

    # The two setup commits are separate steps, not one: the boot snapshot
    # predates the projects, so the project rows sit in the diff between them
    # and neither commit is charged to a chat.
    boot_to_setup = sandbox.diff_summary(clone, boot_sha, setup_sha)
    assert boot_to_setup["added"] == [".runtime/web_projects.json"]
    assert sandbox.diff_summary(clone, baseline, boot_sha)["added"] == [
        "memory-vault/INDEX.md"
    ]

    # The pre-fix wiring really would have shown the setup as chat 1's work.
    stale = sandbox.diff_summary(clone, baseline, chat_sha)
    stale_changed = [*stale["added"], *stale["modified"], *stale["deleted"]]
    assert "memory-vault/INDEX.md" in stale_changed
    assert ".runtime/web_projects.json" in stale_changed


def test_wait_for_boot_idle_waits_for_a_continuous_empty_window() -> None:
    """Boot is not finished when the port answers; it is finished when nothing runs.

    A single empty sample is not enough: a server that has just bound its port
    has not run its first tick yet, and a tick that fires a schedule or the
    startup backfill would write into the agent clone with nothing attributing
    it. So the wait is for a window, and a chat appearing inside the window
    restarts it.
    """

    class FakeInstance:
        """Only ``active_chats`` matters here, and it is scripted per poll."""

        def __init__(self, script: list[set[str]]) -> None:
            self.script = list(script)
            self.polls = 0

        def active_chat_ids(self) -> set[str]:
            self.polls += 1
            if self.script:
                return self.script.pop(0)
            return set()

    # Busy, then empty: the wait must not return on the first empty poll.
    busy_then_idle = FakeInstance([{"chat-1"}, set()])
    run.wait_for_boot_idle(busy_then_idle, seconds=0.0, poll=0.0)  # type: ignore[arg-type]
    assert busy_then_idle.polls >= 3, "an idle window must be more than one sample"

    # A chat that reappears restarts the window rather than being ignored.
    flapping = FakeInstance([{"chat-1"}, set(), {"chat-2"}, set()])
    run.wait_for_boot_idle(flapping, seconds=0.0, poll=0.0)  # type: ignore[arg-type]
    assert flapping.polls >= 5

    # Never idle: a refusal, because writes nobody can attribute are worse than
    # a run that stops.
    never = FakeInstance([{"chat-1"}] * 1000)
    with pytest.raises(sandbox.SandboxError) as excinfo:
        run.wait_for_boot_idle(never, seconds=0.0, timeout=0.05, poll=0.01)  # type: ignore[arg-type]
    assert "active chat" in str(excinfo.value)


# ── token accounting ─────────────────────────────────────────────────────


def test_sum_claude_usage_dedupes_message_ids(tmp_path: Path) -> None:
    """A transcript's usage is counted once per message, not once per record.

    A real ``<session_id>.jsonl`` writes the same assistant record several
    times -- persisted, copied into a sidechain, re-read -- and every copy
    carries the same ``usage`` block. Summing records would report a chat as
    costing three or four times what it did, and a token column that lies is
    worse than one that says it does not know.
    """
    usage = {
        "input_tokens": 4,
        "output_tokens": 120,
        "cache_read_input_tokens": 30_000,
        "cache_creation_input_tokens": 9_500,
    }
    other = {
        "input_tokens": 1,
        "output_tokens": 40,
        "cache_read_input_tokens": 1_000,
        "cache_creation_input_tokens": 250,
    }
    lines = [
        json.dumps({"type": "queue-operation", "operation": "add"}),
        json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}),
        json.dumps({"type": "assistant", "message": {"id": "msg_1", "usage": usage}}),
        # The same message, persisted twice and copied into a sidechain.
        json.dumps(
            {"type": "assistant", "isSidechain": True, "message": {"id": "msg_1", "usage": usage}}
        ),
        json.dumps({"type": "assistant", "message": {"id": "msg_2", "usage": other}}),
        json.dumps({"type": "assistant", "message": {"id": "msg_2", "usage": other}}),
        # A summary record, a truncated line, and an assistant with no usage.
        json.dumps({"type": "summary", "summary": "a session summary"}),
        '{"type": "assistant", "mess',
        json.dumps({"type": "assistant", "message": {"id": "msg_3"}}),
        "",
    ]
    jsonl = tmp_path / "session.jsonl"
    jsonl.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert sandbox.sum_claude_usage(jsonl) == {
        "input_tokens": 5,
        "output_tokens": 160,
        "cache_read_input_tokens": 31_000,
        "cache_creation_input_tokens": 9_750,
    }

    # A record with no message id is still counted, rather than dropped.
    no_id = tmp_path / "no-id.jsonl"
    no_id.write_text(
        json.dumps({"type": "assistant", "message": {"role": "assistant", "usage": usage}})
        + "\n",
        encoding="utf-8",
    )
    assert sandbox.sum_claude_usage(no_id) == usage

    # A missing transcript is zero, not an exception: the caller records what
    # it could not read and the report prints `-` for that chat.
    assert sandbox.sum_claude_usage(tmp_path / "absent.jsonl") == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }


def test_chat_usage_reads_the_transcript_under_the_workspace_agent_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chat's tokens are read from its own working directory, not the install root.

    A harness chat runs with cwd ``config.agent_root(row.workspace)``, and
    Claude encodes *that* into the session's project directory. On a re-rooted
    install the agent root is a subdirectory of the clone, so a transcript
    looked up under the install root's slug does not exist -- and the failure is
    silent: zero tokens for every chat, which reads in the report as "the pilot
    was free" rather than as a bug. The transcript here is placed only under the
    encoded agent-root directory, and the row's workspace is what finds it.
    """
    from ciao.transcripts import _claude_projects_dir

    clone = (tmp_path / "clone").resolve()
    (clone / ".runtime").mkdir(parents=True)
    _write_json(
        clone / ".runtime" / "workspaces.json",
        [{"name": "work", "vault_root": "memory-vault/work"}],
    )
    # The re-rooting receipt is what makes `agent_root` answer per workspace,
    # so the agent root is `<clone>/work` and not the install root.
    _write_json(
        clone / ".runtime" / "migration" / "workspace-rooting.json",
        {"status": "migrated"},
    )
    assert run.clone_config(clone).agent_root("work") == clone / "work"

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    usage = {
        "input_tokens": 7,
        "output_tokens": 120,
        "cache_read_input_tokens": 30_000,
        "cache_creation_input_tokens": 9_500,
    }

    def _write_transcript(session_id: str, counters: dict[str, int]) -> None:
        path = _claude_projects_dir(clone / "work") / f"{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"type": "assistant", "message": {"id": "msg_1", "usage": counters}}
            )
            + "\n",
            encoding="utf-8",
        )

    _write_transcript("sess-1", usage)
    _write_transcript("sess-zero", dict.fromkeys(usage, 0))

    class FakeInstance:
        """Only the session id matters, and only for the chats that have one."""

        sessions = {"chat-1": "sess-1", "chat-3": "sess-zero"}

        def chat(self, chat_id: str) -> dict:
            session_id = self.sessions.get(chat_id)
            return {"session_id": session_id} if session_id else {}

    inst = FakeInstance()
    assert run.chat_usage(inst, clone, "chat-1", "claude", "work") == usage

    # A missing transcript, an all-zero one, and a provider the harness cannot
    # read are all "not read" rather than zero: a truthy dict of four zeroes
    # is totalled by the report like a real measurement.
    assert run.chat_usage(inst, clone, "chat-2", "claude", "work") == {}
    assert run.chat_usage(inst, clone, "chat-3", "claude", "work") == {}
    assert run.chat_usage(inst, clone, "chat-1", "opencode", "work") == {}

    # ... and the report carries the real numbers rather than a dash.
    rows = [
        run.Row(
            chat_id="chat-1",
            workspace="work",
            provider="claude",
            effective_provider="claude",
            model="opus",
            archive_rel=Path("Logs/Chats/chat-1/claude/a.md"),
        )
    ]
    report = run.render_report(
        "pilot-1",
        Path("/live"),
        rows,
        {"agent": [run.ArmResult(chat_id="chat-1", seconds=12.0, usage=usage)]},
        "now",
    )
    assert f"{sum(usage.values()):,}" in report
    assert "| chat-1 | claude | opus | ok |" in report


def test_report_shows_agent_tokens_and_dashes_where_there_are_none() -> None:
    """The report's token columns are the real totals, or an honest `-`.

    A per-chat and an arm total, both empty-dash for a provider whose usage
    cannot be read (opencode), because a partial row that summed silently
    would understate the run it is reporting on.
    """
    usage = {
        "input_tokens": 4,
        "output_tokens": 120,
        "cache_read_input_tokens": 30_000,
        "cache_creation_input_tokens": 9_500,
    }
    rows = [
        run.Row(
            chat_id="chat-1",
            workspace="work",
            provider="claude",
            effective_provider="claude",
            model="opus",
            archive_rel=Path("Logs/Chats/chat-1/claude/a.md"),
        ),
        run.Row(
            chat_id="chat-2",
            workspace="work",
            provider="opencode",
            effective_provider="opencode",
            model="gpt",
            archive_rel=Path("Logs/Chats/chat-2/opencode/b.md"),
        ),
    ]
    arms = {
        "agent": [
            run.ArmResult(chat_id="chat-1", seconds=12.0, usage=usage),
            run.ArmResult(chat_id="chat-2", seconds=9.0),
        ]
    }
    report = run.render_report("pilot-1", Path("/live"), rows, arms, "now")

    header = next(line for line in report.splitlines() if line.startswith("| arm |"))
    assert "tokens" in header and "cache read" in header
    # The arm total is the sum of the one chat that reported.
    assert "39,624" in report
    # Chat 2 has no usage to show.
    chat2_row = next(line for line in report.splitlines() if line.startswith("| chat-2 |"))
    assert chat2_row.count("| - ") >= 1
